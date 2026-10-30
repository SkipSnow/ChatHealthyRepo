# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""index_fda_prescription_records (EPIC-010-F-007) -- stamp a sequential fda_record_number
on the staged PRESCRIPTION openFDA labels so build_fda_label_resolution can
range-partition the FDA work across worker PROCESSES (LLD I.3.4a).

A label is in scope when it carries indications_and_usage prose and is NOT OTC
(openfda.product_type naming a HUMAN OTC DRUG). OTC labels are left unstamped and so are
excluded from the fdaProductToDiagnosisResolution collection. Serial, idempotent
(already-stamped staging is not re-stamped). Reads carry no run_id: a full load
drains-then-replaces the staging collection, so the staged rows are the one truth
regardless of which run staged them (Start At, operator design 2026-10-22).
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


# Sections that appear ONLY on an OTC Drug Facts panel. 21 CFR 201.66
# mandates the Drug Facts headings, and a prescription insert carries none
# of them: an Rx label has indications_and_usage, warnings_and_precautions
# and clinical_pharmacology, never a Purpose or a Keep Out of Reach of
# Children. So a label carrying one of these is an OTC label whether or not
# openfda says so.
_DRUG_FACTS_ONLY_SECTIONS = (
    "purpose",
    "keep_out_of_reach_of_children",
    "do_not_use",
    "when_using",
    "stop_use",
    "ask_doctor",
    "ask_doctor_or_pharmacist",
)


# Narrative sections a Drug Facts panel's content lands in when the author
# does not use the mandated element. Deliberately narrow: not
# indications_and_usage, where an Rx label legitimately writes "do not use".
# Sections where a label states its own prescription status in prose.
# Where an indication may be stated, in the design's own order.
_INDICATION_SECTIONS = ("indications_and_usage", "purpose",
                        "package_label_principal_display_panel")

_RX_STATEMENT_SECTIONS = ("spl_unclassified_section", "description",
                          "how_supplied", "package_label_principal_display_panel")

_PROSE_SECTIONS = ("warnings", "warnings_and_cautions", "other_safety_information",
                   "spl_unclassified_section", "package_label_principal_display_panel")

# Phrases that only a Drug Facts panel carries, as 21 CFR 201.66 words them.
_DRUG_FACTS_PHRASES = ("keep out of reach of children", "do not use",
                       "when using this product", "stop use and ask a doctor",
                       "for external use only", "ask a doctor before use",
                       "uses temporarily relieves", "drug facts")


def _otc_basis(label: dict) -> str | None:
    """Why this label is OTC, or None when it is not.

    openfda.product_type is the flag, and it is absent or silent on a large
    minority of the corpus: the design's settled findings put 48,008 labels
    (18.3%) OTC-FLAGGED against 179,116 unflagged, and name the residual as
    needing "Drug-Facts-panel vs Rx/PI-structure" classification. That
    classification was never implemented, so an unflagged OTC label was
    stamped, built, and published. A titanium-dioxide sunscreen -- "Uses
    Helps prevent sunburn" -- was found resolved in the transient output of
    run 39747592 on 2026-10-29, carrying purpose, do_not_use,
    keep_out_of_reach_of_children and inactive_ingredient: a Drug Facts
    panel in every respect except the one field that was checked.

    The basis is returned rather than a bare bool so the run can count the
    two populations separately and the classification is auditable.
    """
    declared = " ".join(
        str(v).upper() for v in
        (label.get("openfda") or {}).get("product_type") or [])
    if "OTC" in declared:
        return "product_type"
    if "PRESCRIPTION" in declared:
        # Declared Rx is authoritative and no structural or prose guess
        # may overrule it. The prose rule below reads phrases an Rx label
        # writes legitimately -- "Do not use in patients with..." -- and
        # on 2,000 real labels it swept up 45 declared prescription drugs.
        # Excluding a real prescription molecule is worse than admitting
        # an OTC one, so the declaration wins where it exists.
        return None
    # "Rx only" is the prescription statement 21 CFR 201.100 requires, and
    # a label whose openfda block is empty still carries it in its own
    # prose. The Ofloxacin Ophthalmic Solution label of set_id 00011703
    # opens spl_unclassified_section with exactly that, and had no
    # product_type at all -- so the declaration existed and nothing read
    # it. Checked before the OTC guesses, for the same reason a declared
    # product_type is: a statement beats an inference.
    for section in _RX_STATEMENT_SECTIONS:
        value = label.get(section)
        text = (" ".join(str(v) for v in value) if isinstance(value, list)
                else str(value or "")).lower()
        if "rx only" in text or "prescription only" in text:
            return None

    for section in _DRUG_FACTS_ONLY_SECTIONS:
        value = label.get(section)
        if isinstance(value, str):
            if value.strip():
                return f"drug_facts_panel:{section}"
        elif value:
            return f"drug_facts_panel:{section}"
    # The headings again, but as prose. An SPL author may put Drug Facts
    # content inside a generic warnings section instead of the mandated
    # element, and a classifier that reads element NAMES then misses it.
    # Measured 2026-10-29 on 2,000 real labels: 107 of the 505 in-scope
    # unkeyable labels carry these phrases in prose -- 74 of them "do not
    # use" and 22 "keep out of reach" -- having none of the sections.
    # Read only from the narrative sections a Drug Facts panel would use,
    # never from indications prose, so an Rx label that happens to say
    # "do not use in pregnancy" is not swept up.
    for section in _PROSE_SECTIONS:
        value = label.get(section)
        text = (" ".join(str(v) for v in value) if isinstance(value, list)
                else str(value or "")).lower()
        if not text:
            continue
        for phrase in _DRUG_FACTS_PHRASES:
            if phrase in text:
                return f"drug_facts_prose:{phrase.replace(' ', '_')}"
    return None


def _has_indication(label: dict) -> bool:
    """Whether the label states an indication anywhere the design allows."""
    for section in _INDICATION_SECTIONS:
        value = label.get(section)
        text = (" ".join(str(v) for v in value) if isinstance(value, list)
                else str(value or "")).strip()
        if text:
            return True
    return False


def _is_otc(label: dict) -> bool:
    return _otc_basis(label) is not None


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    ofda = reg.by_source_name("openfda_labels")
    staging_coll = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]

    # Build mode: this serial pre-pass drains the FDA leg's per-label result
    # store so the fan-out repopulates it from zero -- drain-then-replace, the
    # same single-truth rule the staging loader applies. Without the drain,
    # run-id-free folds would mix stale labels from an older staging.
    if bool(ctx.args.build_fda_drug):
        from pipeline.MedicareProviderEvaluationDataPipeline.steps.build_fda_label_resolution import (  # noqa: PLC0415
            TRANSIENT_COLLECTION)
        removed = mongo[ofda.staging_db][TRANSIENT_COLLECTION].delete_many({}).deleted_count
        _log.LogPipeline(
            "INFO", "index_fda_prescription_records: drained transient rows=%d", removed)

    already = staging_coll.count_documents(
        {"fda_record_number": {"$exists": True}})
    if already:
        _log.LogPipeline(
            "INFO", "index_fda_prescription_records: already stamped count=%d",
            already)
        return {"step": "index_fda_prescription_records", "fda_records": already,
                "restamped": False}

    staging_coll.create_index([("fda_record_number", 1)])
    number = otc_excluded = no_indication = 0
    ops: list[UpdateOne] = []
    for doc in staging_coll.find({}).sort("_id", 1):
        label = doc.get("raw") or {}
        # The design's settled finding: "package_label_principal_display"
        # "_panel recovers ~99.9% of the small indication-missing
        # residual." It was never consulted, so 9,303 labels were
        # dropped in run 39747592 for want of prose another field
        # carries.
        if not _has_indication(label):
            no_indication += 1
            continue
        if _is_otc(label):
            otc_excluded += 1
            continue
        number += 1
        ops.append(UpdateOne({"_id": doc["_id"]},
                             {"$set": {"fda_record_number": number}}))
        if len(ops) >= 1000:
            staging_coll.bulk_write(ops, ordered=False)
            ops = []
    if ops:
        staging_coll.bulk_write(ops, ordered=False)

    if number == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="index_fda_prescription_records",
            message=("index_fda_prescription_records: no prescription FDA labels "
                     "staged; openFDA staging empty or all OTC"))
    _log.LogPipeline(
        "INFO",
        "index_fda_prescription_records: stamped=%d otc_excluded=%d no_indication=%d",
        number, otc_excluded, no_indication)
    return {"step": "index_fda_prescription_records", "fda_records": number,
            "otc_excluded": otc_excluded, "no_indication": no_indication,
            "restamped": True}


def execute(ctx):
    return run_step(ctx)
