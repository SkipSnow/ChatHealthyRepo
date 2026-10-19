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
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    molecule_id)

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


def _drug_class(product_type) -> str:
    """OTC / RX / UNDETERMINED from openfda.product_type (plain-string, no regex).
    OTC is excluded upstream, so a built record is RX or -- absent a structural
    signal -- UNDETERMINED; never defaulted."""
    vals = product_type if isinstance(product_type, list) else ([product_type] if product_type else [])
    joined = " ".join(str(v).upper() for v in vals)
    if "PRESCRIPTION" in joined:
        return "RX"
    if "OTC" in joined or "OVER-THE-COUNTER" in joined:
        return "OTC"
    return "UNDETERMINED"


def _get_tool() -> FDADrugEnrichmentTool:
    global _TOOL
    if _TOOL is None:
        _TOOL = FDADrugEnrichmentTool(catalog=_CATALOG_PATH)
    return _TOOL


_ACTIVE_INGREDIENT_HEADERS = ("active ingredients", "active ingredient(s)", "active ingredient")


def _molecule_from_active_ingredient(value) -> str:
    """Recover the molecule from raw.active_ingredient when the harmonized openFDA
    molecule (substance_name/generic_name) is absent (FDA field-mining study,
    EPIC-010-F-007; covers ~63% of the no-molecule records). Deterministic, no regex
    (Rule-065 statement 6): strip the 'Active ingredient(s)' header, drop the dose by
    keeping the text up to the first digit, and keep the substance name(s)."""
    items = value if isinstance(value, list) else ([value] if value else [])
    names: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        lowered = text.lower()
        for header in _ACTIVE_INGREDIENT_HEADERS:
            if lowered.startswith(header):
                text = text[len(header):].lstrip(" :.-")
                break
        # one active_ingredient string may list several substances, comma/pipe-separated;
        # split first, then drop each substance's dose (text from its first digit).
        for piece in text.replace("|", ",").split(","):
            piece = piece.strip()
            if not piece:
                continue
            cut = len(piece)
            for i, ch in enumerate(piece):
                if ch.isdigit():
                    cut = i
                    break
            name = piece[:cut].strip(" ,;:-")
            if name:
                names.append(name)
    return ", ".join(names)


def _shape_fda_record(raw: dict) -> dict:
    """Shape a raw openFDA label into the record the enrichment tool requires.

    The tool reads top-level ``molecule`` and ``indication_text`` off each record
    (alongside ``set_id`` and ``openfda``, which the raw label already carries)
    but does not derive them. Map them from the label's canonical fields:
      molecule        <- openfda.substance_name, else openfda.generic_name when
                         substance_name is absent. The generic_name case is a
                         deliberate gap-response, not a silent substitution: the
                         source is recorded in ``_molecule_source`` so run_step
                         can surface it as a finding on the discrepancy report.
      indication_text <- the indications_and_usage prose
    Already-present values are left untouched, so a pre-shaped record (the
    benchmark's own input) passes through unchanged. ``_molecule_source`` is a
    transient shaping marker (leading underscore) read by run_step and not
    persisted on the built record.
    """
    shaped = dict(raw)
    openfda = raw.get("openfda") or {}
    if not str(shaped.get("molecule") or "").strip():
        subs = openfda.get("substance_name") or []
        if isinstance(subs, str):
            subs = [subs]
        subs = [s for s in subs if s]
        if subs:
            shaped["molecule"] = ", ".join(subs)
            shaped["_molecule_source"] = "substance_name"
        else:
            gen = openfda.get("generic_name") or []
            if isinstance(gen, str):
                gen = [gen]
            gen = [g for g in gen if g]
            if gen:
                shaped["molecule"] = ", ".join(gen)
                shaped["_molecule_source"] = "generic_name"
            else:
                # No harmonized molecule: recover it in-label from active_ingredient
                # (first cut; the description path and the spl positional + RxNorm/UNII
                # vocabulary path are the next increments per the field-mining study).
                recovered = _molecule_from_active_ingredient(raw.get("active_ingredient"))
                shaped["molecule"] = recovered
                shaped["_molecule_source"] = "active_ingredient" if recovered else "absent"
    else:
        shaped["_molecule_source"] = "provided"
    if not str(shaped.get("indication_text") or "").strip():
        prose = raw.get("indications_and_usage") or []
        if isinstance(prose, str):
            prose = [prose]
        shaped["indication_text"] = "\n".join(p for p in prose if p)
    return shaped


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

    # The fda_molecule_id join index is provisioned declaratively by the
    # medicare_fda_indexes package in deployment_architecture.json (indexes live
    # in provisioning, not in-run).

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
    records = [
        _shape_fda_record(doc.get("raw") or {})
        for doc in staging_coll.find(
            {"run_id": ctx.run_id, "fda_record_number": {"$gte": start, "$lt": end}}
        ).sort("fda_record_number", 1)
    ]
    if not records:
        return {"step": "build_fda_drug_collection", "record_range": [start, end],
                "records_written": 0}

    # A per-record attribute problem is recorded as a finding and (when the
    # record cannot be built) skipped, so one bad label does not fail the whole
    # run. The molecule-from-generic fallback and the incomplete-record miss are
    # both FDA attribute failures and share one catch-all class on the report.
    buildable = []
    skipped = 0
    for rec in records:
        set_id = str(rec.get("set_id") or "").strip()
        if rec.pop("_molecule_source", None) == "generic_name":
            rt.record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="build_fda_drug_collection", artifact="fda",
                record_key=set_id,
                detail={"kind": "molecule_from_generic_fallback",
                        "note": "molecule taken from openfda.generic_name; "
                                "openfda.substance_name was absent"})
        missing = [f for f in ("molecule", "indication_text")
                   if not str(rec.get(f) or "").strip()]
        if missing:
            rt.record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="build_fda_drug_collection", artifact="fda",
                record_key=set_id,
                detail={"kind": "record_incomplete", "missing": missing})
            skipped += 1
            continue
        buildable.append(rec)
    if not buildable:
        _log.LogPipeline(
            "INFO", "build_fda_drug_collection: range=[%d,%d) all %d record(s) "
            "incomplete; skipped, nothing to build", start, end, skipped)
        return {"step": "build_fda_drug_collection", "record_range": [start, end],
                "records_written": 0, "records_skipped": skipped}

    tool = _get_tool()

    def _sink(record) -> None:
        doc = record.model_dump()
        doc["_id"] = record.set_id            # set_id IS the _id (one record per label)
        doc["run_id"] = ctx.run_id
        doc["data_version"] = data_version
        # molecule_id is the join key the provider x molecule build and enrich use:
        # minted from the FDA clean molecule name by the SAME normalization the CMS
        # Gnrc_Name side uses, so the two sides share one id.
        doc["molecule_id"] = molecule_id(doc.get("molecule"))
        openfda = doc.get("openfda") or {}
        doc["drug_class"] = {"class": _drug_class(openfda.get("product_type"))}
        target_coll.replace_one({"_id": record.set_id}, doc, upsert=True)

    def _on_batch_error(keys, exc) -> None:
        # The model crosswalk failed for this batch; its records are skipped
        # rather than aborting the whole partition. Record one finding so the
        # coverage loss is visible in the run report instead of silently dropped.
        rt.record_discrepancy(
            reason="FDA_all_other_Attribute_failures",
            step="build_fda_drug_collection", artifact="fda",
            record_key=str(getattr(keys[0], "set_id", "") or "") if keys else "",
            detail={"kind": "crosswalk_batch_skipped",
                    "skipped_count": len(keys),
                    "error": str(exc)[:200]})

    produced = tool.enrich(buildable, record_number_start=start, sink=_sink,
                           on_batch_error=_on_batch_error)
    coded = sum(1 for r in produced if any(ind.codes for ind in r.indications))
    _log.LogPipeline(
        "INFO", "build_fda_drug_collection: range=[%d,%d) records=%d with_codes=%d skipped=%d",
        start, end, len(produced), coded, skipped)
    return {"step": "build_fda_drug_collection", "record_range": [start, end],
            "records_written": len(produced), "records_with_codes": coded,
            "records_skipped": skipped}


def execute(ctx):
    return run_step(ctx)
