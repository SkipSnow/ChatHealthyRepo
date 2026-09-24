# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Declare this run's data version once, so version-managed connections resolve
the pre-versioned collection names the pipeline builds.

The pipeline builds one declared generation (data_version). The version resolver
in mongo_utilities, added as a data-safety feature, refuses a pre-versioned name
such as Provider_staging_v_5 unless a binding declares that this IS the version
the runtime reads. Rather than every build-step connection opting out of the
safety feature with manage_versions=False, this declares the run's version once
-- for every dataset -- so the whole build path resolves against it. The
resolver then accepts each pre-versioned name because it matches the binding.

_state is per-process, and bootstrap exec()s into the entry point (which wipes
it), so this runs from the step-execution entry point after the config is in
hand. Idempotent: bound once per process per data_version.
"""
from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.runtime_collections_state import _state

from pipeline_config import ensure_pipeline_config
from pipeline_dataset_registry import PipelineDatasetRegistry

_log = ChatHealthyLoggingService()


def install_version_bindings(data_version: int, pipeline_mongo,
                             env_prefix: str = "dev") -> None:
    """Bind every dataset's base collection to its data_version generation.

    Populates _state.bases so a version-managed connection resolves
    <db>.<base> -> <db>.<base>_v_<data_version>. Runs once per process; a second
    call for the same version returns without work.
    """
    if getattr(_state, "pipeline_bound_version", None) == data_version and _state.bases:
        return
    config = ensure_pipeline_config(pipeline_mongo, env_prefix)
    registry = PipelineDatasetRegistry(config, data_version, pipeline_mongo)
    bases: dict = {}
    for entry in registry.entries():
        bases[(entry.staging_db, entry.staging_coll_base)] = (
            f"{entry.staging_db}.{entry.staging_coll_base}_v_{data_version}")
        bases[(entry.public_data_db, entry.public_data_coll_base)] = (
            f"{entry.public_data_db}.{entry.public_data_coll_base}_v_{data_version}")
    _state.bases = bases
    _state.pipeline_bound_version = data_version
    _log.info("pipeline version binding installed: data_version=%d datasets=%d",
              data_version, len(registry.entries()))
