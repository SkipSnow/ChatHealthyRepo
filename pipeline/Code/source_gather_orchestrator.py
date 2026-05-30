# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""source_gather_orchestrator - fan-out of source-gather activities.

nppes_zip lands the NPPES dissemination zip into the provider-data blob
container. pl_pfile depends on that same zip — it streams the Practice
Location Reference file out of it. Running them in parallel races on the
shared download_zip_fn path (same blob, same temp file pattern), which
in the past caused the gather_pl_pfile_activity worker to die with
SIGTERM. nppes_zip therefore runs FIRST and to completion; the other
five sources (pl_pfile, zip_crosswalk, rucc, specialty_catalog, icd10)
fan out in parallel after nppes_zip lands. Each acquisition still goes
through @throttle@source_gather for startup-rate control.
"""
from __future__ import annotations


_NPPES_ZIP_SOURCE = "nppes_zip"

_PARALLEL_SOURCES = (
    "pl_pfile",
    "zip_crosswalk",
    "rucc",
    "specialty_catalog",
    "icd10",
)


def source_gather_orchestrator_fn(context):
    from throttle_acquire import acquire

    cfg = context.get_input() or {}

    # Phase 1: nppes_zip must land before anything else can read it.
    yield from acquire(context, "source_gather", n=1)
    yield context.call_activity(f"gather_{_NPPES_ZIP_SOURCE}_activity", cfg)

    # Phase 2: the remaining five sources are independent of each other.
    tasks = []
    for src in _PARALLEL_SOURCES:
        yield from acquire(context, "source_gather", n=1)
        tasks.append(context.call_activity(f"gather_{src}_activity", cfg))
    yield context.task_all(tasks)
    return {"sources_loaded": 1 + len(tasks)}
