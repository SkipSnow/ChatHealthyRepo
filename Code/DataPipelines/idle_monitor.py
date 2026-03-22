# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""IdleMonitor — auto-pauses ChatHealthyDataPipelines when idle too long.

Runs on a timer. Checks cluster state via Atlas API, then queries
admin.PipelineDiscrepancyReport for the last completed run. If the cluster
has been idle longer than IDLE_MONITOR_THRESHOLD_HOURS (default 2), it is
paused and a SparkPost email is sent.

Environment variables:
    IDLE_MONITOR_CLUSTER       Cluster to monitor (default: ChatHealthyDataPipelines)
    IDLE_MONITOR_THRESHOLD_HOURS  Hours of inactivity before auto-pause (default: 2)
"""

import logging
import os
from datetime import datetime, timezone

from pymongo import MongoClient
from sparkpost import SparkPost

from atlas_cluster_manager import get_cluster_info, scale_down

_mongo: MongoClient | None = None

REPORT_COLLECTION = "admin.PipelineDiscrepancyReport"


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


def check_and_pause() -> dict:
    """Check the pipeline cluster for idle activity and pause if warranted.

    Returns a dict describing the action taken (or not taken).
    """
    cluster = os.environ.get("IDLE_MONITOR_CLUSTER", "ChatHealthyDataPipelines")
    threshold_hours = float(os.environ.get("IDLE_MONITOR_THRESHOLD_HOURS", "2"))

    # ── 1. Check cluster state ────────────────────────────────────────────────
    info = get_cluster_info(cluster)

    if info["paused"]:
        logging.info("IdleMonitor: %s is already paused.", cluster)
        return {"action": "none", "reason": "already_paused"}

    if info["state"] != "IDLE":
        logging.info("IdleMonitor: %s is %s — skipping check.", cluster, info["state"])
        return {"action": "none", "reason": f"cluster_{info['state'].lower()}"}

    # ── 2. Check for active load workers ─────────────────────────────────────
    # status_code=10 means "Load invoked" — worker is actively writing to staging.
    # Pausing while workers are running causes InterruptedDueToReplStateChange.
    meta_db, meta_coll = "admin", "DataLoadMetadata"
    active_workers = _get_mongo_client()[meta_db][meta_coll].count_documents(
        {"status_code": 10}
    )
    if active_workers > 0:
        logging.info(
            "IdleMonitor: %d active load worker(s) detected — skipping pause.", active_workers
        )
        return {"action": "none", "reason": "load_in_progress", "active_workers": active_workers}

    # ── 3. Query last completed pipeline run ──────────────────────────────────
    db_name, coll_name = REPORT_COLLECTION.split(".", 1)
    last_report = _get_mongo_client()[db_name][coll_name].find_one(
        {}, sort=[("datetime", -1)]
    )

    if not last_report:
        logging.info("IdleMonitor: No pipeline reports found — not pausing.")
        return {"action": "none", "reason": "no_reports"}

    last_dt = datetime.fromisoformat(last_report["datetime"])
    if last_dt.tzinfo is None:
        last_dt = last_dt.replace(tzinfo=timezone.utc)

    idle_hours = (datetime.now(timezone.utc) - last_dt).total_seconds() / 3600

    if idle_hours < threshold_hours:
        logging.info(
            "IdleMonitor: %s — last activity %.1fh ago, threshold %.1fh — no action.",
            cluster, idle_hours, threshold_hours,
        )
        return {"action": "none", "reason": "below_threshold", "idle_hours": round(idle_hours, 2)}

    # ── 3. Pause and notify ───────────────────────────────────────────────────
    logging.warning(
        "IdleMonitor: %s idle %.1fh (threshold %.1fh) — pausing.",
        cluster, idle_hours, threshold_hours,
    )
    scale_down(cluster)
    _send_notification(cluster, idle_hours)

    return {"action": "paused", "cluster": cluster, "idle_hours": round(idle_hours, 2)}


def _send_notification(cluster: str, idle_hours: float) -> None:
    api_key    = os.environ.get("SPARKMAIL_API_KEY")
    from_email = os.environ.get("NOTIFICATION_FROM_EMAIL")
    to_email   = os.environ.get("NOTIFICATION_TO_EMAIL")

    if not (api_key and from_email and to_email):
        logging.warning("IdleMonitor: SparkPost credentials not configured — skipping email.")
        return

    try:
        sp = SparkPost(api_key)
        sp.transmissions.send(
            recipients=[to_email],
            from_email=from_email,
            subject=f"{cluster} auto-paused (idle {idle_hours:.1f}h)",
            text=(
                f"{cluster} was idle for {idle_hours:.1f} hours.\n"
                f"The cluster has been automatically paused to avoid unnecessary charges.\n\n"
                f"It will resume automatically when the next pipeline run starts (ScaleUp)."
            ),
        )
        logging.info("IdleMonitor: notification sent to %s.", to_email)
    except Exception as exc:
        logging.error("IdleMonitor: SparkPost send failed: %s", exc)
