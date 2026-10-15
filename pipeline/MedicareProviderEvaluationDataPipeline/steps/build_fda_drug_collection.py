# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_fda_drug_collection (EPIC-010-F-007) -- build the fdaProductToDiagnosisResolution
collection, ChatHealthy's FDA drug collection: one record per FDA product (set_id), carrying
its resolved RxNorm ingredient RXCUIs and the ICD-10-CM diagnoses each indication treats.

Process-based fan-out (LLD I.3.4a). The step runs under process_pool parallelism: one worker
PROCESS per record-number range. The concurrency is the number of worker processes (sized to
the worker box), NOT threads -- each worker drains its range serially, calling
FDADrugEnrichmentTool (one pydantic-ai agent, two single-concern calls). The ICD-10 catalog
loads once per process and is reused across every range that process drains; OpenAI prompt
caching, being server-side and organization-scoped, then serves every process after the
first, so per-process catalog loading costs no extra cache misses beyond a handful.

The record-number ranges come from the index_fda_prescription_records prerequisite, which
stamps fda_record_number on this run's PRESCRIPTION labels only (OTC excluded). This step
reads its assigned range from that stamped set.

Build-or-consume is the operator's per-run argument (BUILD_FDA_DRUG): off -> consume the
published collection (a single verify partition, no mining); on -> each worker builds its
range as a candidate for the operator-gated data release (EPIC-010-F-003).
"""

from __future__ import annotations

import os

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.fda_drug_enrichment_tool import (
    FDADrugEnrichmentTool)

_log = ChatHealthyLoggingService()

# The proven ICD-10-CM 3/4-char catalog shipped with the pipeline. It is the model's cached
# static prefix, byte-identical to the catalog the benchmark proved.
_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "icd10_categories_top3levels.xlsx")

# Per-process tool cache: a worker process drains many record-range work_items; the catalog
# loads once per process and is reused across every range the worker handles.
_TOOL = None


def _truthy(raw: str) -> bool:
    return raw.strip().lower() in ("1", "true", "yes")


def _get_tool() -> FDADrugEnrichmentTool:
    global _TOOL
    if _TOOL is None:
        _TOOL = FDADrugEnrichmentTool(catalog=_CATALOG_PATH)
    return _TOOL


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    data_version = int(ctx.args.data_version)

    tgt = reg.by_source_name("fda_drug")
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("fda_drug")]

    # Read the build-or-consume flag from the run's args, which round-trip to
    # worker processes through the work-item snapshot. The old os.environ read
    # only worked on the Controller: workers run on a separate VM and never
    # inherited BUILD_FDA_DRUG, so every partition fell into consume mode and
    # the build never happened.
    building = bool(ctx.args.build_fda_drug)
    if not building:
        published = target_coll.count_documents({})
        if published == 0:
            raise ChatHealthyException(
                mode="runtime_error",
                component="build_fda_drug_collection",
                message=("build_fda_drug_collection: the run did not ask to build the "
                         "fdaProductToDiagnosisResolution collection and none is published "
                         "to consume; fire with --build-fda-drug to build it"))
        _log.LogPipeline(
            "INFO", "build_fda_drug_collection: consume mode; published records=%d",
            published)
        return {"step": "build_fda_drug_collection", "rebuild": False, "consumed": published}

    partition = (ctx.config or {}).get("partition") or {}
    rng = partition.get("record_range")
    if not rng:
        raise ChatHealthyException(
            mode="runtime_error",
            component="build_fda_drug_collection",
            message=("build_fda_drug_collection: a build runs under process_pool and each "
                     "worker requires a record_range partition; none was supplied"))
    start, end = int(rng["start"]), int(rng["end"])

    ofda = reg.by_source_name("openfda_labels")
    staging_coll = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]
    records = [doc.get("raw") or {} for doc in staging_coll.find(
        {"run_id": ctx.run_id, "fda_record_number": {"$gte": start, "$lt": end}}
    ).sort("fda_record_number", 1)]
    if not records:
        return {"step": "build_fda_drug_collection", "record_range": [start, end],
                "records_written": 0}

    tool = _get_tool()

    def _sink(record) -> None:
        doc = record.model_dump()
        doc["run_id"] = ctx.run_id
        doc["data_version"] = data_version
        target_coll.replace_one({"set_id": record.set_id}, doc, upsert=True)

    produced = tool.enrich(records, record_number_start=start, sink=_sink)
    coded = sum(1 for r in produced if any(ind.codes for ind in r.indications))
    _log.LogPipeline(
        "INFO", "build_fda_drug_collection: range=[%d,%d) records=%d with_codes=%d",
        start, end, len(produced), coded)
    return {"step": "build_fda_drug_collection", "record_range": [start, end],
            "records_written": len(produced), "records_with_codes": coded}


def execute(ctx):
    return run_step(ctx)
