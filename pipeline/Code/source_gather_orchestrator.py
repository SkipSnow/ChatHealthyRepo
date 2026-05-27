# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""source_gather_orchestrator - parallel fan-out of the six source-gather activities."""
from __future__ import annotations


_SOURCES = (
    "nppes_zip",
    "pl_pfile",
    "zip_crosswalk",
    "rucc",
    "specialty_catalog",
    "icd10",
)


def source_gather_orchestrator_fn(context):
    import azure.durable_functions as df
    cfg = context.get_input() or {}
    throttle_id = df.EntityId("throttle", "source_gather")
    tasks = []
    for src in _SOURCES:
        yield context.call_entity(
            throttle_id,
            "request_permission",
            {
                "n": 1,
                "request_id": f"{context.instance_id}:{src}",
                "callback_instance_id": context.instance_id,
            },
        )
        tasks.append(context.call_activity(f"gather_{src}_activity", cfg))
    yield context.task_all(tasks)
    return {"sources_loaded": len(tasks)}
