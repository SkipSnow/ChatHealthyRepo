# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""build_fda_label_resolution (EPIC-010-F-007, LLD v83 III.10) -- the per-label
resolution fan-out, the first of the two formal FDA build steps (operator
ruling 2026-10-20).

One worker PROCESS per record-number range (LLD I.3.4a; width from
fan_out.medicare.build_fda_label_resolution). Each worker resolves its range's
in-scope (non-OTC) labels exactly as before -- deterministic molecule recovery,
the ICD-10 crosswalk, the gated LLM pass via FDADrugEnrichmentTool -- and
writes each label's RESULT to the transient collection, idempotently ($set
upsert keyed by set_id). It publishes NOTHING: the serial finalize step
(finalize_fda_molecules) groups the results by molecule_id and writes the
molecule documents, so every published write is single-writer and the fold is
exactly-once by construction.

The record-number ranges come from the index_fda_prescription_records
prerequisite, which stamps fda_record_number on this run's PRESCRIPTION labels
only (OTC excluded). Build-or-consume is the operator's per-run argument
(BUILD_FDA_DRUG): off -> consume the published collection (verified by the
finalize step); on -> build.
"""

from __future__ import annotations

import datetime
import os

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.fda_drug_enrichment_tool import (
    FDADrugEnrichmentTool)

_log = ChatHealthyLoggingService()

# The transient per-label result collection (LLD v83 III.10): per-run scratch
# on the staging database, dropped at quiesce. One document per set_id.
TRANSIENT_COLLECTION = "TransientFdaLabelResolution"

# The proven ICD-10-CM 3/4-char catalog shipped with the pipeline. It is the model's cached
# static prefix, byte-identical to the catalog the benchmark proved.
_CATALOG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data", "icd10_categories_top3levels.xlsx")

# Per-process tool cache: a worker process drains many record-range work_items; the catalog
# loads once per process and is reused across every range the worker handles.
_TOOL = None


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


def _require_fda_levers(ctx) -> dict:
    """The fda lever block from the per-env durable configuration (LLD v83
    III.15). REQUIRED -- a missing value is a hard fail (H7: no defaults)."""
    fda = (ctx.config or {}).get("fda")
    retry = (fda or {}).get("llm_retry") if isinstance(fda, dict) else None
    if (not isinstance(fda, dict)
            or not isinstance(fda.get("crosswalk_batch_size"), int)
            or not isinstance(retry, dict)
            or not isinstance(retry.get("max_transient_retries"), int)
            or not isinstance(retry.get("backoff_base_s"), (int, float))):
        raise ChatHealthyException(
            mode="pipeline_config_missing_value",
            message=("fda{crosswalk_batch_size, llm_retry{max_transient_retries, "
                     "backoff_base_s}} is required in the per-env pipeline "
                     "configuration and is absent or malformed (H7: no defaults)"),
            component="build_fda_label_resolution",
        )
    return fda


def _get_tool(ctx) -> FDADrugEnrichmentTool:
    global _TOOL
    if _TOOL is None:
        fda = _require_fda_levers(ctx)
        _TOOL = FDADrugEnrichmentTool(
            catalog=_CATALOG_PATH,
            batch_size=int(fda["crosswalk_batch_size"]),
            max_transient_retries=int(fda["llm_retry"]["max_transient_retries"]),
            backoff_base_s=float(fda["llm_retry"]["backoff_base_s"]),
        )
    return _TOOL


_ACTIVE_INGREDIENT_HEADERS = ("active ingredients", "active ingredient(s)", "active ingredient")

# A Drug Facts dosing preamble sits in front of the substance name as a
# parenthetical -- "(in each gram) Bacitracin zinc", "(in each caplet)",
# "(in each patch) Nicotine" -- and survived both the header strip and the
# cut-at-first-digit rule, so 297 molecules reached
# fdaProductToDiagnosisResolution with an _id beginning '('. One carried
# 1,400 characters of inactive-ingredient prose as its key.
_PREAMBLE_OPEN = "("
_PREAMBLE_CLOSE = ")"

# A molecule key is a name. Anything longer is prose that escaped the parse.
_MAX_MOLECULE_NAME = 120

# Words that mark the text a dosage or section statement, not a substance.
# Pharmaceutical excipients. active_ingredient is supposed to carry the
# actives, but a Drug Facts panel whose prose lands in that field offers
# the vehicle as well -- water, glycerin, sodium chloride -- and a wrong
# key is worse than no key: it publishes a drug record under a solvent.
# The parse proposes, this list refuses. It is NOT a vocabulary check: the
# design pairs the SPL parse with RxNorm for that, and this is the
# deterministic floor under it.
_EXCIPIENTS = frozenset({
    "water", "purified water", "water for injection", "sterile water",
    "glycerin", "glycerol", "sodium chloride", "hydrochloric acid",
    "sodium hydroxide", "benzalkonium chloride", "sucrose", "lactose",
    "mannitol", "titanium dioxide", "magnesium stearate", "talc",
    "gelatin", "starch", "cellulose", "microcrystalline cellulose",
    "hydroxypropyl cellulose", "povidone", "croscarmellose sodium",
    "silicon dioxide", "propylene glycol", "polyethylene glycol",
    "alcohol", "ethyl alcohol", "isopropyl alcohol", "petrolatum",
    "mineral oil", "carbomer", "edetate disodium", "citric acid",
    "sodium citrate", "sodium benzoate", "methylparaben",
    "propylparaben", "xanthan gum", "sorbitol", "dextrose",
})

_NOT_A_SUBSTANCE = ("in each", "per tablet", "per capsule", "per ml",
                    "per dose", "inactive ingredient", "purpose", "uses")


def _strip_dose_preamble(text: str) -> str:
    """Drop leading parenthetical dosing phrases, however many there are."""
    out = text.strip()
    while out.startswith(_PREAMBLE_OPEN):
        close = out.find(_PREAMBLE_CLOSE)
        if close == -1:
            return ""
        out = out[close + 1:].strip()
    return out



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
            piece = _strip_dose_preamble(piece)
            if not piece:
                continue
            cut = len(piece)
            for i, ch in enumerate(piece):
                if ch.isdigit():
                    cut = i
                    break
            name = piece[:cut].strip(" ,;:-")
            # A key is a name. Prose that escaped the parse is not a
            # molecule and must never become an _id.
            if not name or len(name) > _MAX_MOLECULE_NAME:
                continue
            lowered_name = name.lower().strip(" .,;:")
            if any(w in lowered_name for w in _NOT_A_SUBSTANCE):
                continue
            if lowered_name in _EXCIPIENTS:
                continue
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
    shaping marker (leading underscore) read by run_step and persisted to the
    transient result as molecule_source for the finalize union.
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
                if recovered:
                    shaped["molecule"] = recovered
                    shaped["_molecule_source"] = "active_ingredient"
                else:
                    shaped["molecule"] = ""
                    shaped["_molecule_source"] = "absent"
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

    building = bool(ctx.args.build_fda_drug)
    if not building:
        # Consume mode: the finalize step verifies the published collection;
        # nothing for the per-label fan-out to do.
        return {"step": "build_fda_label_resolution", "rebuild": False}

    partition = (ctx.config or {}).get("partition") or {}
    rng = partition.get("record_range")
    if not rng:
        raise ChatHealthyException(
            mode="runtime_error",
            component="build_fda_label_resolution",
            message=("build_fda_label_resolution: a build runs under process_pool and each "
                     "worker requires a record_range partition; none was supplied"))
    start, end = int(rng["start"]), int(rng["end"])

    ofda = reg.by_source_name("openfda_labels")
    staging_db = mongo[ofda.staging_db]
    staging_coll = staging_db[reg.staging_collection_name("openfda_labels")]
    transient_coll = staging_db[TRANSIENT_COLLECTION]
    records = [
        _shape_fda_record(doc.get("raw") or {})
        for doc in staging_coll.find(
            {"fda_record_number": {"$gte": start, "$lt": end}}
        ).sort("fda_record_number", 1)
    ]
    if not records:
        return {"step": "build_fda_label_resolution", "record_range": [start, end],
                "labels_resolved": 0}

    # A per-record attribute problem is recorded as a finding and (when the
    # record cannot be built) skipped, so one bad label does not fail the whole
    # run. The molecule-from-generic fallback and the incomplete-record miss are
    # both FDA attribute failures and share one catch-all class on the report.
    buildable = []
    sources_by_set_id: dict[str, str] = {}
    skipped = 0
    for rec in records:
        set_id = str(rec.get("set_id") or "").strip()
        source = rec.pop("_molecule_source", None)
        sources_by_set_id[set_id] = source or "absent"
        if source == "generic_name":
            rt.record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="build_fda_label_resolution", artifact="fda",
                record_key=set_id,
                detail={"kind": "molecule_from_generic_fallback",
                        "note": "molecule taken from openfda.generic_name; "
                                "openfda.substance_name was absent"})
        missing = [f for f in ("molecule", "indication_text")
                   if not str(rec.get(f) or "").strip()]
        if missing:
            # No key, no record -- operator ruling 2026-10-29. But a label
            # that cannot be keyed HERE is judged at the earliest moment of
            # the run, before the RxNorm vocabulary is staged, so it is held
            # rather than discarded and re-asked at enrichment. Previously
            # the only trace was this finding: 63,178 of them in run
            # 39747592, and the labels themselves went nowhere.
            rt.record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="build_fda_label_resolution", artifact="fda",
                record_key=set_id,
                detail={"kind": "record_incomplete", "missing": missing})
            skipped += 1
            continue
        buildable.append(rec)
    if not buildable:
        _log.LogPipeline(
            "INFO", "build_fda_label_resolution: range=[%d,%d) all %d record(s) "
            "incomplete; skipped, nothing to resolve", start, end, skipped)
        return {"step": "build_fda_label_resolution", "record_range": [start, end],
                "labels_resolved": 0, "labels_skipped": skipped}

    tool = _get_tool(ctx)

    def _sink(record) -> None:
        # One transient result per label, idempotent by set_id ($set upsert):
        # a re-driven range overwrites its own results byte-identically. The
        # published molecule documents are written ONLY by the serial finalize.
        doc = record.model_dump()
        doc["run_id"] = ctx.run_id
        doc["molecule_source"] = sources_by_set_id.get(str(record.set_id), "absent")
        openfda = doc.get("openfda") or {}
        doc["drug_class"] = _drug_class(openfda.get("product_type"))
        doc["resolved_at"] = datetime.datetime.utcnow()
        transient_coll.update_one(
            {"_id": record.set_id}, {"$set": doc}, upsert=True)

    def _on_batch_error(keys, exc) -> None:
        # The model crosswalk failed for this batch; its records are skipped
        # rather than aborting the whole partition. Record one finding so the
        # coverage loss is visible in the run report instead of silently dropped.
        rt.record_discrepancy(
            reason="FDA_all_other_Attribute_failures",
            step="build_fda_label_resolution", artifact="fda",
            record_key=str(getattr(keys[0], "set_id", "") or "") if keys else "",
            detail={"kind": "crosswalk_batch_skipped",
                    "skipped_count": len(keys),
                    "error": str(exc)[:200]})

    # Durable per-batch telemetry (operator-ordered 2026-10-21): one small doc
    # per LLM batch into the coordination store, so batch-size economics
    # (seconds/label, failures/label by batch size) are answerable by query
    # across runs. Best-effort: a telemetry write failure logs a WARNING and
    # never discards a paid model result.
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: PLC0415
    llm_batches = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["pipeline.llm_batches"]

    def _on_batch_usage(batch_meta: dict) -> None:
        doc = dict(batch_meta)
        doc["run_id"] = ctx.run_id
        doc["step"] = "build_fda_label_resolution"
        doc["record_range"] = [start, end]
        doc["ts"] = datetime.datetime.utcnow()
        try:
            llm_batches.insert_one(doc)
        except Exception as exc:  # noqa: BLE001
            _log.LogPipeline(
                "WARNING", "build_fda_label_resolution: llm_batches telemetry "
                "write failed (batch continues): %s", str(exc)[:200])

    produced = tool.enrich(buildable, record_number_start=start, sink=_sink,
                           on_batch_error=_on_batch_error,
                           on_batch_usage=_on_batch_usage)
    coded = sum(1 for r in produced if any(ind.codes for ind in r.indications))
    _log.LogPipeline(
        "INFO", "build_fda_label_resolution: range=[%d,%d) labels=%d with_codes=%d skipped=%d",
        start, end, len(produced), coded, skipped)
    return {"step": "build_fda_label_resolution", "record_range": [start, end],
            "labels_resolved": len(produced), "labels_with_codes": coded,
            "labels_skipped": skipped}


def execute(ctx):
    return run_step(ctx)
