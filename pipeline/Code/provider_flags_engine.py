# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Proprietary provider-flag enrichment.

Stamps on every provider record in the run:

  can_prescribe        (bool)  Level 1 credential parse: any provider whose
                               provider_credential_text parses to MD or DO
                               gets can_prescribe=True regardless of
                               taxonomy. Level 2: primary taxonomy code
                               lookup in the normalized specialty catalog.
  is_homeopathic       (bool)  Primary taxonomy code lookup in normalized
                               specialty catalog.

Sourced from:
  - Specialty catalog    PipelinePublicHealthData.SpecialtyMetaData on the
                         pipelines cluster. Published by normalize_nucc
                         (Provider Pipeline) with 883 NUCC codes + N F-105
                         supplements, all normalized to top-level Code /
                         can_prescribe / is_homeopathic / is_supplemented.
  - NPPES NPI staging    PublicStaging.StagingProvider_v_{data_version}

Discipline: no fallbacks. Any collection-empty, code-not-in-catalog,
credential-not-parseable, or row-field-missing condition raises
ChatHealthyException.
"""

from __future__ import annotations


import time as _time
from datetime import datetime, timezone
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

from typing import Any

from pymongo import UpdateOne
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities


_log = ChatHealthyLoggingService()


def _record_discrepancy(run_id: str, entry: dict) -> None:
    """Record one unresolved taxonomy code encountered during a run.

    This used to signal a Durable Functions entity named work_manager, which
    no target has hosted since the Function App was retired. It was then
    reduced to a log line, which meant every unresolved taxonomy code was
    written down where no report reads and none of them ever reached the
    operator. It is persisted now, to the same collection every other
    discrepancy uses. See LLD v42 sec. 6.9 Data Quality and
    NUCC_SpecialtyCodeDataDiscrepancyManagement.docx for governance.
    """
    doc = {
        "run_id": run_id,
        "npi": entry.get("npi"),
        "reason": entry.get("reason", "unresolved_taxonomy_code"),
        "step": "provider_flags_enrichment",
        "state": entry.get("state"),
        "entity_kind": entry.get("entity_kind"),
        "level": "warning",
        "detail": {"code": entry.get("code"), "message": entry.get("message")},
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]["pipeline.discrepancies"].insert_one(doc)
    except Exception as exc:  # noqa: BLE001 - a lost discrepancy must not end the run
        _log.warning(
            "provider_flags discrepancy could not be persisted run_id=%s npi=%s "
            "code=%s (%s); it is recorded here only",
            run_id, entry.get("npi"), entry.get("code"), exc)


DEFAULT_BATCH = 500

# Credentials with universal US prescribing authority. Parsed from the
# provider_credential_text string on the provider record. Match is
# case-insensitive and token-based (whitespace/punctuation split) so
# "MD, PhD" / "M.D." / "MD, FACS" / "DO" all resolve.
_PRESCRIBING_CREDENTIAL_TOKENS = frozenset({"MD", "DO"})


def _raise_mongo_client_required() -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="mongo_client_required",
        message=(
            "provider_flags_engine: pipeline-cluster Mongo client is "
            "required to read PipelinePublicHealthData.SpecialtyMetaData_v_"
            "{data_version} (the loaded catalog produced by "
            "publish_smd_and_embed). Caller must pass mongo= kwarg."
        ),
    )


def _raise_catalog_row_missing_code(coll_ref: str, row: dict) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="catalog_row_missing_code",
        message=(f"provider_flags_engine: {coll_ref} row _id="
                 f"{row.get('_id')} is missing 'Code' (top level)"),
        collection=coll_ref,
        keys=sorted(list(row.keys())),
    )


def _raise_catalog_row_missing_flag(coll_ref: str, code, flag: str,
                                    mode_suffix: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode=f"catalog_row_missing_{mode_suffix}",
        message=(f"provider_flags_engine: {coll_ref} row for "
                 f"Code={code!r} is missing {flag!r} (top level)"),
        collection=coll_ref, code=code,
    )


def _raise_catalog_empty(coll_ref: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="catalog_empty",
        message=(f"provider_flags_engine: {coll_ref} loaded zero catalog "
                 f"rows -- publish_smd_and_embed must have failed to land "
                 f"the loaded collection. F-105 flag enrichment cannot "
                 f"proceed."),
        collection=coll_ref,
    )


def _load_catalog(mongo, data_version: int, state: str = 'ALL') -> dict[str, dict[str, bool]]:
    """Load the normalized specialty catalog into
    {code -> {can_prescribe, is_homeopathic, is_supplemented}}.

    Reads from ChatHealthyDataPipelines.PipelinePublicHealthData.SpecialtyMetaData_v_
    {data_version} on the PIPELINE cluster (operator directive 2026-08-02:
    pipelines never read data collections from the front-end cluster; the
    canonical PipelinePublicHealthData collection is produced by
    publish_smd_and_embed on the pipeline cluster). Fields are at TOP LEVEL of
    every doc (Code / can_prescribe / is_homeopathic / is_supplemented). Raises
    if collection is empty or any row is missing an expected field -- no
    fallback."""
    coll_name = f"SpecialtyMetaData_v_{data_version}"
    coll = mongo["PipelinePublicHealthData"][coll_name]
    coll_ref = f"PipelinePublicHealthData.{coll_name}"
    out: dict[str, dict[str, bool]] = {}
    _started = _time.time()
    _log.info("provider_flags[%s]: loading catalog from %s", state, coll_ref)
    for row in coll.find({}):
        code = row.get("Code") or row.get("code")
        can_prescribe = row.get("can_prescribe")
        is_homeopathic = row.get("is_homeopathic")
        is_supplemented = row.get("is_supplemented")
        if not code:
            _raise_catalog_row_missing_code(coll_ref, row)
        if can_prescribe is None:
            _raise_catalog_row_missing_flag(coll_ref, code, "can_prescribe",
                                            "can_prescribe")
        if is_homeopathic is None:
            _raise_catalog_row_missing_flag(coll_ref, code, "is_homeopathic",
                                            "homeopathic")
        out[str(code).strip()] = {
            "can_prescribe": bool(can_prescribe),
            "is_homeopathic": bool(is_homeopathic),
            "is_supplemented": bool(is_supplemented),
        }
    if not out:
        _raise_catalog_empty(coll_ref)
    return out


def _primary_taxonomy_code(doc: dict) -> str:
    """Return the primary taxonomy code for a provider.

    Reads the top-level primary_taxonomy_code field, which is stamped by
    normalize_provider_rows from the NPPES per-slot Primary Taxonomy
    Switch (per NPPES Readme v.2 sec. 1.1, semantically per-NPI).

    Raises if the field is missing -- no fallback to taxonomies[0].code
    because a missing primary designator means either NPPES didn't
    provide one (data-quality issue to surface) or normalize failed to
    lift it (code bug). Silent fallback would mask both."""
    code = (doc.get("primary_taxonomy_code") or "").strip()
    if code:
        return code
    raise ChatHealthyException(
        mode="provider_missing_primary_taxonomy_code",
        message=(
            f"provider_flags_engine: provider npi={doc.get('npi')!r} has "
            f"no top-level primary_taxonomy_code. Either NPPES did not "
            f"designate a primary taxonomy (source-data issue) or "
            f"normalize_provider_rows failed to lift it from the raw "
            f"Primary Taxonomy Switch columns."
        ),
        npi=doc.get("npi"),
    )


def _credential_grants_prescribing(credential_text: str | None) -> bool:
    """Level 1 rule: True iff the provider credential parses to MD or DO.

    Tokenization: uppercase, strip periods, split on comma+whitespace.
    "MD" / "M.D." / "MD, PhD" / "MD, FACS" / "DO" / "DO, MPH" all match.
    Empty / None / any other credential text returns False -- caller then
    falls through to the catalog (level 2)."""
    if not credential_text:
        return False
    # Strip periods (M.D. -> MD, D.O. -> DO); split on comma and whitespace
    # to get token set. Uppercase-normalize so match is case-insensitive.
    normalized = credential_text.replace(".", "").upper()
    tokens = {t.strip() for t in normalized.replace(",", " ").split()}
    return bool(tokens & _PRESCRIBING_CREDENTIAL_TOKENS)


def _stamp_taxonomy_status(doc: dict, catalog: dict) -> None:
    """Stamp `status` on doc['taxonomies'][i] ONLY for codes that are not
    normal NUCC-backed. Absent = normal (default); 'Supplemented' = code
    exists in F-105 as an operational supplement (F-105 is the single
    source of truth for its metadata -- provider records do NOT duplicate
    grouping/classification/specialization/definition); 'Missing' = code
    is in neither current NUCC nor F-105.

    Ceremonial bytes matter across 9.25M records -- normal records stay
    untouched, supplemented records add exactly one field (status). See
    NUCC_SpecialtyCodeDataDiscrepancyManagement.docx."""
    for tax in doc.get("taxonomies") or []:
        code = (tax.get("code") or "").strip()
        if not code:
            continue
        entry = catalog.get(code)
        if entry is None:
            tax["status"] = "Missing"
            continue
        if entry.get("is_supplemented"):
            tax["status"] = "Supplemented"
            # No description peers on the provider record -- F-105 is the
            # sole source of truth for grouping/classification/etc per
            # operator directive 2026-08-01.
        # Normal NUCC-backed case: leave the taxonomy element untouched.


def _apply_flags_to_doc(
    doc: dict,
    *,
    catalog: dict[str, dict[str, object]],
    discrepancy_sink=None,
) -> dict[str, bool] | None:
    """Deterministic flag computation.

    Also mutates doc['taxonomies'] in place to stamp `taxonomy_source`
    on every taxonomy element (enum: Present | Supplemented | Missing).
    Supplemented taxonomies also receive their NUCC-equivalent
    description fields from the F-105 supplement entry.

    When the primary taxonomy code is absent from the F-105 catalog:
      - If `discrepancy_sink` was provided, invoke it with a
        {reason, code, npi, message} entry and return None. The caller
        skips flag stamping for this record and continues; upstream
        report_discrepancy() persists the entry to the discrepancy
        report per LLD v42 §6.9 (Data Quality — NUCC supplement pattern).
      - If no sink was provided, raise (pre-supplement-pattern behavior).
    """
    # Stamp taxonomies[i].status ONLY on Supplemented or Missing codes
    # (absent = normal NUCC-backed; ~99.999% of records). See
    # NUCC_SpecialtyCodeDataDiscrepancyManagement.docx.
    _stamp_taxonomy_status(doc, catalog)

    code = _primary_taxonomy_code(doc)
    if code not in catalog:
        if discrepancy_sink is not None:
            # Signal shape per LLD v41 §8.4 (rewritten 2026-08-01) --
            # level / source_line / npi / field / explanation flow
            # straight into the discrepancy_report.pdf body row.
            discrepancy_sink({
                "level": "error",
                "reason": "unresolved_taxonomy_code",
                "source_line": doc.get("source_line"),
                "npi": doc.get("npi"),
                "field": "Healthcare Provider Taxonomy Code_1",
                "code": code,
                "explanation": (
                    f"Primary taxonomy code {code!r} is in neither current "
                    f"NUCC nor the F-105 supplement catalog. Flag stamping "
                    f"skipped; code recorded for curator review."
                ),
            })
            return None
        raise ChatHealthyException(
            mode="taxonomy_code_missing_from_catalog",
            message=(
                f"provider_flags_engine: primary taxonomy code {code!r} "
                f"on npi={doc.get('npi')!r} is not present in the F-105 "
                f"catalog. Either the catalog is stale or the provider "
                f"carries an invalid NUCC code."
            ),
            npi=doc.get("npi"), code=code,
        )
    cat_row = catalog[code]
    credential_prescribes = _credential_grants_prescribing(
        doc.get("provider_credential_text")
    )
    can_prescribe = credential_prescribes or cat_row["can_prescribe"]
    is_homeopathic = cat_row["is_homeopathic"]
    return {
        "can_prescribe": can_prescribe,
        "is_homeopathic": is_homeopathic,
    }


def apply_provider_flags(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Stamp the flags on every provider record in the run.

    Required config:
      - run_id                (str)
      - data_version          (int)
      - provider_collection   (str "<db>.<coll>")
      - entity_kind_filter    (str | None) - "individual" | "institutional"
      - partition_state       (str | None) - restrict to this business state
      - batch_size            (int, default 500)

    Returns metrics dict. Unresolved taxonomy codes (present in neither
    current NUCC nor the F-105 supplement catalog) DO NOT abort the run:
    the record's flag stamping is skipped and a discrepancy entry is
    emitted via report_discrepancy() for the tail-of-run discrepancy
    report (LLD v42 sec. 6.9 Data Quality / see
    NUCC_SpecialtyCodeDataDiscrepancyManagement.docx). Artifact-level
    defects (missing catalog, empty staging, misconfigured provider
    doc) still raise ChatHealthyException."""
    run_id = config["run_id"]
    data_version = int(config["data_version"])
    provider_collection = config["provider_collection"]
    entity_kind_filter = config.get("entity_kind_filter")
    partition_state = (config.get("partition_state") or "").upper() or None
    batch_size = int(config.get("batch_size", DEFAULT_BATCH))

    if mongo is None:
        _raise_mongo_client_required()
    _t0 = _time.time()
    _state_label_start = partition_state or "ALL"
    _log.info("provider_flags[%s]: START run_id=%s entity=%s "
              "collection=%s batch=%d",
              _state_label_start, run_id, entity_kind_filter or "ALL",
              provider_collection, batch_size)
    _state_label = partition_state or "ALL"
    catalog = _load_catalog(mongo, data_version, _state_label)
    _log.info("provider_flags[%s]: catalog ready in %.0fs (catalog=%s)",
              _state_label, _time.time() - _t0, f"{len(catalog):,}")

    def _sink(entry: dict) -> None:
        _record_discrepancy(run_id, entry)

    db_name, coll_name = provider_collection.split(".", 1)
    coll = mongo[db_name][coll_name]

    query: dict[str, Any] = {"run_id": run_id}
    if entity_kind_filter == "individual":
        query["entity_type_code"] = "1"
    elif entity_kind_filter == "institutional":
        query["entity_type_code"] = "2"
    # NPI-atomic ownership: one worker per state owns every NPI whose
    # BUSINESS mailing address is in that state. Per LLD: business
    # address is the canonical NPI-atomic partition key (single-valued
    # per NPI; practice is optional and multi-valued). Match the shape
    # every other partition-aware engine uses.
    from steps._partitions import business_state_filter  # noqa: PLC0415
    query.update(business_state_filter(partition_state))

    ops: list[UpdateOne] = []
    modified = 0
    matched = 0
    unresolved = 0

    projection = {
        "npi": 1,
        "primary_taxonomy_code": 1,
        "taxonomies": 1,
        "provider_credential_text": 1,
    }
    _scan_started = _time.time()
    _log.info("provider_flags[%s]: scanning %s query=%s",
              _state_label, provider_collection, query)
    for doc in coll.find(query, projection):
        matched += 1
        if matched % 100_000 == 0:
            _log.info("provider_flags[%s]: %s matched, %s modified, "
                      "%s unresolved (%.0fs)", _state_label,
                      f"{matched:,}", f"{modified:,}", f"{unresolved:,}",
                      _time.time() - _scan_started)
        flags = _apply_flags_to_doc(
            doc,
            catalog=catalog,
            discrepancy_sink=_sink,
        )
        if flags is None:
            # Unresolved code -- sink already recorded it; skip stamping.
            unresolved += 1
            continue
        # Clear any stale is_disqualified from prior engine versions --
        # no source-file field currently drives it, so we do not stamp it.
        ops.append(UpdateOne(
            {"_id": doc["_id"]},
            {"$set": flags, "$unset": {"is_disqualified": ""}},
        ))
        if len(ops) >= batch_size:
            result = coll.bulk_write(ops, ordered=False)
            modified += (result.modified_count or 0)
            ops = []

    if ops:
        result = coll.bulk_write(ops, ordered=False)
        modified += (result.modified_count or 0)

    _log.info("provider_flags[%s]: DONE matched=%s modified=%s "
              "unresolved=%s in %.0fs (scan %.0fs)",
              _state_label, f"{matched:,}", f"{modified:,}",
              f"{unresolved:,}", _time.time() - _t0,
              _time.time() - _scan_started)
    return {
        "matched": matched,
        "modified": modified,
        "unresolved_taxonomy_count": unresolved,
        "catalog_size": len(catalog),
    }
