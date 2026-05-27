# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""source_gather_orchestrator - parallel fan-out of the six source-gather activities.

Acquires one token from @throttle@source_gather per source before scheduling
each activity. Uses the canonical orchestrator-side acquire helper.
"""
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
    from throttle_acquire import acquire

    cfg = context.get_input() or {}
    tasks = []
    for src in _SOURCES:
        yield from acquire(context, "source_gather", n=1)
        tasks.append(context.call_activity(f"gather_{src}_activity", cfg))
    yield context.task_all(tasks)
    return {"sources_loaded": len(tasks)}
