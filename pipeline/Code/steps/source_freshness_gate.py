# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""source_freshness_gate — per-source reuse vs fetch decisions."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

_log = logging.getLogger(__name__)


def execute(ctx) -> dict:
    from pipeline_db import get_mongo

    registry = get_mongo()["admin"]["DataSourceRegistry"]
    freshness = ctx.config.get("source_freshness") or []
    decisions: dict[str, str] = {}
    now = datetime.now(timezone.utc)

    for entry in freshness:
        name = entry["source_name"]
        ttl_days = int(entry["number_of_days"])
        doc = registry.find_one({"source_name": name}) or {}
        last = doc.get("last_archived_at")
        if last and isinstance(last, datetime):
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            if now - last < timedelta(days=ttl_days):
                decisions[name] = "reuse"
                continue
        decisions[name] = "fetch"

    ctx.manifest.metrics["source_freshness"] = decisions
    return {"decisions": decisions}


def run_step(ctx) -> dict:
    return execute(ctx)
