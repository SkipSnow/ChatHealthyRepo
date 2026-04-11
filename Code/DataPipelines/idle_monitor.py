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

REPORT_COLLECTION = "admin.PipelineDiscrepancyReports"


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


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
