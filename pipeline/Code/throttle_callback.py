# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""throttle_callback - Storage Queue listener used by process_assignment_activity.

The activity polls a per-orchestration queue at 200 ms cadence (per design §14)
for grant events written by throttle_entity. Each message is
{"request_id": str, "granted": bool, "n": int}.
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid

_POLL_INTERVAL_SECONDS = 0.2
_DEFAULT_WAIT_TIMEOUT_SECONDS = 600.0


def _safe_queue_name(raw: str) -> str:
    out_chars = []
    for ch in raw.lower():
        if ch.isalnum() or ch == "-":
            out_chars.append(ch)
        else:
            out_chars.append("-")
    name = "".join(out_chars).strip("-")
    while "--" in name:
        name = name.replace("--", "-")
    if len(name) < 3:
        name = (name + "-throttle")[:63]
    return name[:63]


def _queue_client(orchestration_instance_id: str):
    from azure.storage.queue import QueueClient
    conn = os.environ.get("AZURE_STORAGE_CONNECTION_STRING") or os.environ.get("AzureWebJobsStorage")
    if not conn:
        raise RuntimeError("throttle_callback: storage connection string missing")
    queue_name = _safe_queue_name(f"throttle-callbacks-{orchestration_instance_id}")
    client = QueueClient.from_connection_string(conn, queue_name)
    try:
        client.create_queue()
    except Exception:
        pass
    return client


def request_permission_and_wait(
    throttle_name: str,
    n: int,
    orchestration_instance_id: str,
    timeout_seconds: float = _DEFAULT_WAIT_TIMEOUT_SECONDS,
) -> dict:
    request_id = uuid.uuid4().hex
    client = _queue_client(orchestration_instance_id)
    _signal_throttle(
        throttle_name=throttle_name,
        n=n,
        request_id=request_id,
        callback_instance_id=orchestration_instance_id,
    )
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        messages = client.receive_messages(max_messages=8, visibility_timeout=30)
        for msg in messages:
            try:
                payload = json.loads(msg.content)
            except Exception:
                client.delete_message(msg)
                continue
            if payload.get("request_id") == request_id:
                client.delete_message(msg)
                return payload
            client.delete_message(msg)
        time.sleep(_POLL_INTERVAL_SECONDS)
    raise TimeoutError(
        f"throttle_callback: did not receive grant for {throttle_name} request_id={request_id} "
        f"within {timeout_seconds}s"
    )


def _signal_throttle(throttle_name: str, n: int, request_id: str, callback_instance_id: str) -> None:
    """One-way HTTP signal to a throttle entity via the Durable Functions
    management API. Activities are stateless and cannot call_entity; the HTTP
    signal is the supported workaround."""
    import urllib.parse
    import urllib.request

    host = os.environ.get("WEBSITE_HOSTNAME")
    code = os.environ.get("DURABLE_MGMT_CODE")
    task_hub = os.environ.get("DURABLE_TASK_HUB") or "DevPipelineNetherite2"
    connection = os.environ.get("DURABLE_TASK_CONNECTION") or "Storage"
    if not host or not code:
        raise RuntimeError(
            "throttle_callback: WEBSITE_HOSTNAME and DURABLE_MGMT_CODE app settings required"
        )
    url = (
        f"https://{host}/runtime/webhooks/durabletask/entities/throttle/"
        f"{urllib.parse.quote(throttle_name)}"
        f"?taskHub={urllib.parse.quote(task_hub)}"
        f"&connection={urllib.parse.quote(connection)}"
        f"&code={urllib.parse.quote(code)}"
        f"&op=request_permission"
    )
    body = json.dumps({
        "n": n,
        "request_id": request_id,
        "callback_instance_id": callback_instance_id,
    }).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            _ = r.read()
    except Exception as exc:
        logging.error("throttle_callback: signal failed for %s: %s", throttle_name, exc)
        raise
