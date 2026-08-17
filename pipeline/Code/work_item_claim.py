# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Mongo work_items claim protocol — LLD v22 §2.4."""

from __future__ import annotations
from chathealthy_lib.exceptions import ChatHealthyException

import os
from datetime import datetime, timedelta, timezone

# Coordination is metadata, so it lives in pipelineAdmin on the front-end
# cluster, which answers while the pipeline cluster sleeps. The old value was
# a CLUSTER name used as a database name and no identity is granted on it.


def _coll(mongo_client):
    return mongo_client["pipelineAdmin"]["pipeline.work_items"]


def seed_work_items(
    mongo_client,
    *,
    run_id: str,
    step: str,
    partitions: list[dict],
    lease_seconds: int = 600,
) -> int:
    coll = _coll(mongo_client)
    now = datetime.now(timezone.utc)
    docs = []
    for part in partitions:
        docs.append({
            "run_id": run_id,
            "step": step,
            "partition": part,
            "status": "pending",
            "created_at": now,
            "lease_seconds": lease_seconds,
            "worker_id": None,
            "claimed_at": None,
            "finished_at": None,
            "result": None,
            "attempts": 0,
        })
    if not docs:
        return 0
    coll.insert_many(docs)
    return len(docs)


def claim_work_item(mongo_client, *, run_id: str, step: str, worker_id: str) -> dict | None:
    coll = _coll(mongo_client)
    now = datetime.now(timezone.utc)
    return coll.find_one_and_update(
        {"run_id": run_id, "step": step, "status": "pending"},
        {
            "$set": {
                "status": "claimed",
                "worker_id": worker_id,
                "claimed_at": now,
            },
            "$inc": {"attempts": 1},
        },
        sort=[("created_at", 1)],
        return_document=True,
    )


def complete_work_item(mongo_client, *, item_id, result: dict | None = None) -> None:
    coll = _coll(mongo_client)
    coll.update_one(
        {"_id": item_id},
        {
            "$set": {
                "status": "completed",
                "finished_at": datetime.now(timezone.utc),
                "result": result or {},
            }
        },
    )


def fail_work_item(mongo_client, *, item_id, error: str) -> None:
    coll = _coll(mongo_client)
    coll.update_one(
        {"_id": item_id},
        {
            "$set": {
                "status": "failed",
                "finished_at": datetime.now(timezone.utc),
                "error": error[:500],
            }
        },
    )


def wait_for_step_completion(mongo_client, *, run_id: str, step: str, timeout_seconds: int = 7200) -> dict:
    coll = _coll(mongo_client)
    deadline = datetime.now(timezone.utc) + timedelta(seconds=timeout_seconds)
    while datetime.now(timezone.utc) < deadline:
        pending = coll.count_documents({"run_id": run_id, "step": step, "status": {"$in": ["pending", "claimed"]}})
        if pending == 0:
            failed = coll.count_documents({"run_id": run_id, "step": step, "status": "failed"})
            completed = coll.count_documents({"run_id": run_id, "step": step, "status": "completed"})
            if failed:
                raise ChatHealthyException(mode="runtime_error", message=f"step {step}: {failed} partitions failed ({completed} completed)")
            return {"completed": completed, "failed": failed}
        import time
        time.sleep(5)
    raise TimeoutError(f"step {step} did not complete within {timeout_seconds}s")
