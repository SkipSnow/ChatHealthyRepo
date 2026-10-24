# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""CMS_NPI_PrescriberBase (EPIC-010-F-007-S-002) -- the base build fan-out.

One worker per CMS state. The worker reads the staged Part D rows whose own
Prscrbr_State_Abrvtn is its state -- the CMS file is the only source this task
consults -- groups them by (NPI, molecule) and applies one contribution per
group (REQ-B-001).

The row carries its own partition key, so there is no stamping pre-pass and no
Provider-collection lookup: measured 2026-10-23, not one NPI's rows carry more
than one CMS state across all 28,023,892 staged rows.
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    build_prescriber_base)

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    partition = ctx.config.get("partition") or {}
    state = partition.get("cms_state") if isinstance(partition, dict) else None
    if not state:
        raise ChatHealthyException(
            mode="value_error",
            message=("CMS_NPI_PrescriberBase: expected "
                     "ctx.config['partition']['cms_state']"),
            component="build_providermedicare_base",
            partition=repr(partition),
        )

    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client

    partd = reg.by_source_name("medicare_partd")
    partd_coll = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
    tgt = reg.by_source_name("providermedicare")
    target_coll = mongo[tgt.public_data_db][
        reg.public_data_collection_name("providermedicare")]

    result = build_prescriber_base(
        state=str(state),
        partd_coll=partd_coll,
        target_coll=target_coll,
    )
    _log.LogPipeline("INFO", "CMS_NPI_PrescriberBase[%s]: %s",
                     result.get("partition"), result)
    return result


def execute(ctx):
    return run_step(ctx)
