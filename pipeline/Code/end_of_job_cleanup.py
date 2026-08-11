# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""end_of_job_cleanup — purge all per-job transient state.

Skip's invariant: Durable state is transient. After a job ends — Completed,
Failed, or Terminated — the only thing that survives is the Azure Container
App itself. No orchestrations, no sub-orchs, no entities (work_manager,
throttles), no staging blobs. This activity makes that true.

Called as the LAST step of provider_pipeline_orchestrator's finally block.

Three sweeps, each best-effort and idempotent:
  1) Staging blobs   — delete every blob under pipeline-staging/<load_id>/
  2) Sub-orchs       — terminate + purge any non-terminal Durable instance
                       whose input.load_id matches this run
  3) Per-job entities — terminate + purge @work_manager@<load_id> and every
                       @throttle@<name> created by this job

Failures inside this activity do NOT raise — the job's terminal state is
already decided; cleanup is opportunistic. Every failure is logged with
enough detail (instance id, path, exception) for post-mortem.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import json

import os
import time

import requests


_MGMT_TIMEOUT_S = 30
_MGMT_RETRIES = 4
_MGMT_RETRY_BACKOFF_S = 8


def end_of_job_cleanup_fn(config: dict) -> dict:
    """Run all three sweeps. Returns a dict of counters for the run metric."""
    load_id = config["load_id"]
    throttle_names = list(config.get("throttle_names") or [])
    blob_container = config.get("staging_container", "pipeline-staging")

    counters = {
        "load_id": load_id,
        "staging_blobs_deleted": 0,
        "staging_errors": 0,
        "sub_orchs_terminated": 0,
        "sub_orch_errors": 0,
        "entities_terminated": 0,
        "entity_errors": 0,
    }

    _delete_staging_blobs(load_id, blob_container, counters)
    _sweep_sub_orchestrations(load_id, counters)
    _purge_per_job_entities(load_id, throttle_names, counters)

    ChatHealthyLoggingService().info("end_of_job_cleanup: %s", counters)
    return counters


# ─── Sweep 1: staging blobs under load_id/ ──────────────────────────────

def _delete_staging_blobs(load_id: str, container_name: str, counters: dict) -> None:
    from blob_client import get_blob_service

    try:
        client = get_blob_service().get_container_client(container_name)
    except Exception as exc:
        counters["staging_errors"] += 1
        ChatHealthyLoggingService().warning(
            "end_of_job_cleanup: container client failed: %s", exc,
        )
        return

    batch: list[str] = []
    prefix = f"{load_id}/"
    try:
        for b in client.list_blobs(name_starts_with=prefix):
            batch.append(b.name)
            if len(batch) >= 256:
                counters["staging_blobs_deleted"] += _flush_blob_batch(client, batch, counters)
                batch = []
        if batch:
            counters["staging_blobs_deleted"] += _flush_blob_batch(client, batch, counters)
    except Exception as exc:
        counters["staging_errors"] += 1
        ChatHealthyLoggingService().warning(
            "end_of_job_cleanup: list_blobs(%s) raised: %s", prefix, exc,
        )


def _flush_blob_batch(client, batch: list[str], counters: dict) -> int:
    try:
        client.delete_blobs(*batch)
        return len(batch)
    except Exception as exc:
        counters["staging_errors"] += 1
        ChatHealthyLoggingService().warning(
            "end_of_job_cleanup: delete_blobs batch of %d failed: %s",
            len(batch), exc,
        )
        return 0


# ─── Sweep 2: sub-orchestrations whose input.load_id matches this run ───

def _sweep_sub_orchestrations(load_id: str, counters: dict) -> None:
    code, site = os.environ.get("DURABLE_MGMT_CODE"), os.environ.get("WEBSITE_HOSTNAME")
    if not (code and site):
        ChatHealthyLoggingService().warning(
            "end_of_job_cleanup: DURABLE_MGMT_CODE/WEBSITE_HOSTNAME unset; "
            "skipping sub-orch sweep for %s", load_id,
        )
        return

    base = f"https://{site}/runtime/webhooks/durabletask"
    list_url = (
        f"{base}/instances"
        f"?code={code}"
        "&runtimeStatus=Running,Pending,ContinuedAsNew,Suspended"
        "&showInput=true&top=200"
    )

    instances = _mgmt_get_json(list_url)
    if instances is None:
        counters["sub_orch_errors"] += 1
        return

    for inst in instances:
        iid = inst.get("instanceId") or ""
        if not iid or iid.startswith("@"):
            continue  # entities handled in sweep 3
        if iid == load_id:
            continue  # this orchestration is still in its finally — don't kill self
        if _input_load_id(inst) != load_id:
            continue
        if _mgmt_terminate(base, code, iid, "end_of_job_cleanup"):
            _mgmt_purge(base, code, iid)
            counters["sub_orchs_terminated"] += 1
        else:
            counters["sub_orch_errors"] += 1


def _input_load_id(inst: dict) -> str | None:
    raw = inst.get("input")
    if raw is None:
        return None
    if isinstance(raw, dict):
        v = raw.get("load_id")
        return v if isinstance(v, str) and v else None
    if isinstance(raw, str):
        try:
            d = json.loads(raw)
        except Exception:
            return None
        if isinstance(d, dict):
            v = d.get("load_id")
            return v if isinstance(v, str) and v else None
    return None


# ─── Sweep 3: per-job entities — @work_manager@<load_id> + this job's throttles ───

def _purge_per_job_entities(load_id: str, throttle_names: list[str], counters: dict) -> None:
    code, site = os.environ.get("DURABLE_MGMT_CODE"), os.environ.get("WEBSITE_HOSTNAME")
    if not (code and site):
        ChatHealthyLoggingService().warning(
            "end_of_job_cleanup: DURABLE_MGMT_CODE/WEBSITE_HOSTNAME unset; "
            "skipping entity sweep for %s", load_id,
        )
        return

    base = f"https://{site}/runtime/webhooks/durabletask"
    targets = [f"@work_manager@{load_id}"]
    for name in throttle_names:
        targets.append(f"@throttle@{name}")

    for iid in targets:
        if _mgmt_terminate(base, code, iid, "end_of_job_cleanup"):
            _mgmt_purge(base, code, iid)
            counters["entities_terminated"] += 1
        else:
            counters["entity_errors"] += 1


# ─── Management API helpers ─────────────────────────────────────────────

def _mgmt_get_json(url: str):
    for i in range(_MGMT_RETRIES):
        try:
            r = requests.get(url, timeout=_MGMT_TIMEOUT_S)
            if r.status_code == 200:
                return r.json() or []
            ChatHealthyLoggingService().warning("end_of_job_cleanup: GET %s -> %d", _redact(url), r.status_code)
        except Exception as exc:
            ChatHealthyLoggingService().warning("end_of_job_cleanup: GET %s -> %s", _redact(url), exc)
        time.sleep(_MGMT_RETRY_BACKOFF_S)
    return None


def _mgmt_terminate(base: str, code: str, iid: str, reason: str) -> bool:
    url = f"{base}/instances/{iid}/terminate?code={code}&reason={reason}"
    for i in range(_MGMT_RETRIES):
        try:
            r = requests.post(url, timeout=_MGMT_TIMEOUT_S)
            if r.status_code in (200, 202, 204, 410):
                return True
            ChatHealthyLoggingService().warning(
                "end_of_job_cleanup: terminate %s -> %d (try %d)", iid, r.status_code, i + 1,
            )
        except Exception as exc:
            ChatHealthyLoggingService().warning(
                "end_of_job_cleanup: terminate %s -> %s (try %d)", iid, exc, i + 1,
            )
        time.sleep(_MGMT_RETRY_BACKOFF_S)
    return False


def _mgmt_purge(base: str, code: str, iid: str) -> bool:
    url = f"{base}/instances/{iid}?code={code}"
    for i in range(_MGMT_RETRIES):
        try:
            r = requests.delete(url, timeout=_MGMT_TIMEOUT_S)
            if r.status_code in (200, 202, 204, 404):
                return True
            ChatHealthyLoggingService().warning(
                "end_of_job_cleanup: purge %s -> %d (try %d)", iid, r.status_code, i + 1,
            )
        except Exception as exc:
            ChatHealthyLoggingService().warning(
                "end_of_job_cleanup: purge %s -> %s (try %d)", iid, exc, i + 1,
            )
        time.sleep(_MGMT_RETRY_BACKOFF_S)
    return False


def _redact(url: str) -> str:
    # Strip the mgmt code from any logged URL.
    if "code=" not in url:
        return url
    head, _, tail = url.partition("code=")
    rest = tail.split("&", 1)
    return head + "code=***" + (("&" + rest[1]) if len(rest) > 1 else "")
