# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""instance_warmer.py — pre-warm Flex Consumption instances before fan-out.

Sets always_ready = num_workers via the Azure Management API (Managed Identity),
waits 3× the PATCH acknowledgement latency for propagation, then resets to 0
after the fan-out completes.

Eliminates the two-wave cold-start pattern: without pre-warming, Flex Consumption
spins up instances incrementally, stacking multiple activity workers per instance
and causing OOM kills (exit code 137) and reduced concurrency.

Prerequisites (one-time setup):
  1. System-assigned Managed Identity enabled on the function app.
  2. MSI assigned Website Contributor role on the function app resource itself.
  3. AZURE_SUBSCRIPTION_ID and AZURE_RESOURCE_GROUP in app settings.

Instrumentation written to admin.WarmUpMetrics:
  t0_patch_issued     — PATCH request sent
  t1_patch_acked      — Azure returned 200
  t2_fanout_released  — warm wait complete, fan-out released
  t5_fanout_complete  — task_all returned (written by cool_instances_fn)
  ack_latency_sec     — t1 - t0 (raw PATCH round-trip)
  actual_wait_sec     — 3 × ack_latency_sec (floor: WARM_WAIT_FLOOR_SEC)
"""

import logging
import os
import time
from datetime import datetime, timezone

import requests

log = logging.getLogger(__name__)

# Wait at least this long after PATCH ACK regardless of round-trip latency.
# Flex Consumption cold starts take 45-60s — floor must exceed that.
WARM_WAIT_FLOOR_SEC = 60
PROPAGATION_MULTIPLIER = 3


def _msi_token() -> str:
    resp = requests.get(
        "http://169.254.169.254/metadata/identity/oauth2/token"
        "?api-version=2019-08-01&resource=https://management.azure.com/",
        headers={"Metadata": "true"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _patch_always_ready(token: str, count: int) -> None:
    sub  = os.environ["AZURE_SUBSCRIPTION_ID"]
    rg   = os.environ["AZURE_RESOURCE_GROUP"]
    site = os.environ["WEBSITE_SITE_NAME"]   # injected by the platform

    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Web/sites/{site}"
        "?api-version=2023-12-01"
    )
    body = {
        "properties": {
            "functionAppConfig": {
                "scaleAndConcurrency": {
                    "alwaysReady": (
                        [{"name": "provider_worker_activity", "instanceCount": count}]
                        if count > 0 else []
                    )
                }
            }
        }
    }
    resp = requests.patch(
        url, json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()


def warm_instances_fn(config: dict) -> dict:
    """Set always_ready = num_workers, wait 3× PATCH latency, record timestamps."""
    num_workers = config.get("num_workers", 32)
    metrics: dict = {
        "load_id": config.get("load_id"),
        "num_workers": num_workers,
        "t0_patch_issued": datetime.now(timezone.utc).isoformat(),
    }

    try:
        token = _msi_token()
        t0 = time.monotonic()
        _patch_always_ready(token, num_workers)
        ack_sec = time.monotonic() - t0

        wait_sec = max(ack_sec * PROPAGATION_MULTIPLIER, WARM_WAIT_FLOOR_SEC)

        metrics["t1_patch_acked"]   = datetime.now(timezone.utc).isoformat()
        metrics["ack_latency_sec"]  = round(ack_sec, 2)
        metrics["actual_wait_sec"]  = round(wait_sec, 2)

        log.info(
            "always_ready=%d set (ack %.1fs). Waiting %.0fs for propagation.",
            num_workers, ack_sec, wait_sec,
        )
        time.sleep(wait_sec)

        metrics["t2_fanout_released"] = datetime.now(timezone.utc).isoformat()
        log.info("Warm-up complete. Releasing fan-out.")

    except Exception as exc:
        # Non-fatal: log and proceed. Fan-out will still work; just no pre-warming.
        log.warning("Warm-up skipped (%s) — MSI role may not be assigned yet.", exc)
        metrics["warm_up_skipped"] = str(exc)
        metrics["t2_fanout_released"] = datetime.now(timezone.utc).isoformat()

    _write_metrics(metrics)
    return metrics


def cool_instances_fn(config: dict) -> dict:
    """Reset always_ready = 0 after fan-out completes."""
    metrics: dict = {
        "load_id": config.get("load_id"),
        "t5_fanout_complete": datetime.now(timezone.utc).isoformat(),
    }

    try:
        token = _msi_token()
        _patch_always_ready(token, 0)
        metrics["t6_cooled"] = datetime.now(timezone.utc).isoformat()
        log.info("always_ready reset to 0.")
    except Exception as exc:
        log.warning("Cool-down failed (%s) — always_ready may need manual reset.", exc)
        metrics["cool_down_error"] = str(exc)

    _write_metrics(metrics)
    return metrics


def _write_metrics(metrics: dict) -> None:
    try:
        from provider_load_manager import _get_mongo_client
        _get_mongo_client()["admin"]["WarmUpMetrics"].insert_one({
            **metrics,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        log.warning("WarmUpMetrics write failed: %s", exc)
