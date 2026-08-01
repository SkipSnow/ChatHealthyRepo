# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Proprietary provider-flag enrichment.

Stamps on every provider record in the run:

  can_prescribe        (bool)  Level 1 credential parse: any provider whose
                               provider_credential_text parses to MD or DO
                               gets can_prescribe=True regardless of
                               taxonomy. Level 2: primary taxonomy code
                               lookup in the F-105 catalog's can_prescribe.
  is_homeopathic       (bool)  Primary taxonomy code lookup in F-105
                               catalog's homeopathic field.
  is_npi_registered    (bool)  True iff the NPI appears in the NPPES
                               staging load and is not deactivated.

Sourced from:
  - F-105 catalog        PublicStaging.StagingSpecialtyCatalog_v_{data_version}
                         (rows loaded from SPECIALTY_CLASSIFICATION_CATALOG_URL
                         at fetch time, raw={"Code","can_prescribe","homeopathic"})
  - NPPES NPI staging    PublicStaging.StagingProvider_v_{data_version}

Discipline: no fallbacks. Any collection-empty, code-not-in-catalog,
credential-not-parseable, or row-field-missing condition raises
ChatHealthyException. A missing catalog row for a taxonomy code that
appeared on a provider is a bug in the catalog (or in the taxonomy
code on the provider) that MUST surface, not a silent False.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

from typing import Any

from pymongo import UpdateOne

from staging_loader import STAGING_DB_NAME, staging_collection_name

_log = ChatHealthyLoggingService()

NPPES_DEACTIVATION_KEYS = (
    "NPI Deactivation Date",
    "npi_deactivation_date",
    "Provider Deactivation Date",
)
DEFAULT_BATCH = 500

# Credentials with universal US prescribing authority. Parsed from the
# provider_credential_text string on the provider record. Match is
# case-insensitive and token-based (whitespace/punctuation split) so
# "MD, PhD" / "M.D." / "MD, FACS" / "DO" all resolve.
_PRESCRIBING_CREDENTIAL_TOKENS = frozenset({"MD", "DO"})


def _load_catalog(mongo, data_version: int) -> dict[str, dict[str, bool]]:
    """Load F-105 catalog rows into {code -> {can_prescribe, is_homeopathic}}.

    Reads from PublicStaging.StagingSpecialtyCatalog_v_{data_version}.
    Raises if the collection is empty or any row is missing an expected
    field. There is no fallback -- an empty catalog or a malformed row
    means the fetch/stage step failed silently, and every downstream
    determination would be wrong."""
    coll_name = staging_collection_name("specialty_catalog", data_version)
    coll = mongo[STAGING_DB_NAME][coll_name]
    out: dict[str, dict[str, bool]] = {}
    for row in coll.find({}):
        raw = row.get("raw")
        if not isinstance(raw, dict):
            raise ChatHealthyException(
                mode="catalog_row_malformed",
                message=(
                    f"provider_flags_engine: {STAGING_DB_NAME}.{coll_name} "
                    f"row _id={row.get('_id')} has no 'raw' subdocument"
                ),
                collection=coll_name,
            )
        code = raw.get("Code")
        can_prescribe = raw.get("can_prescribe")
        is_homeopathic = raw.get("homeopathic")
        if not code:
            raise ChatHealthyException(
                mode="catalog_row_missing_code",
                message=(
                    f"provider_flags_engine: {STAGING_DB_NAME}.{coll_name} "
                    f"row _id={row.get('_id')} raw is missing 'Code'"
                ),
                collection=coll_name,
                raw_keys=sorted(list(raw.keys())),
            )
        if can_prescribe is None:
            raise ChatHealthyException(
                mode="catalog_row_missing_can_prescribe",
                message=(
                    f"provider_flags_engine: {STAGING_DB_NAME}.{coll_name} "
                    f"row for Code={code!r} is missing 'can_prescribe'"
                ),
                collection=coll_name, code=code,
            )
        if is_homeopathic is None:
            raise ChatHealthyException(
                mode="catalog_row_missing_homeopathic",
                message=(
                    f"provider_flags_engine: {STAGING_DB_NAME}.{coll_name} "
                    f"row for Code={code!r} is missing 'homeopathic'"
                ),
                collection=coll_name, code=code,
            )
        out[str(code).strip()] = {
            "can_prescribe": bool(can_prescribe),
            "is_homeopathic": bool(is_homeopathic),
        }
    if not out:
        raise ChatHealthyException(
            mode="catalog_empty",
            message=(
                f"provider_flags_engine: {STAGING_DB_NAME}.{coll_name} "
                f"loaded zero catalog rows -- fetch/stage step must have "
                f"failed. F-105 flag enrichment cannot proceed."
            ),
            collection=coll_name,
        )
    return out


def _load_registered_npis(mongo, data_version: int) -> set[str]:
    """Every NPI in the NPPES staging load that is not deactivated."""
    coll_name = staging_collection_name("nppes_npi", data_version)
    coll = mongo[STAGING_DB_NAME][coll_name]
    out: set[str] = set()
    for row in coll.find({}, {"npi": 1, "raw": 1}):
        npi = row.get("npi")
        raw = row.get("raw") or {}
        if not npi:
            npi = raw.get("NPI") or raw.get("npi")
        if not npi:
            continue
        deactivated = any(
            str(raw.get(k) or "").strip() for k in NPPES_DEACTIVATION_KEYS
        )
        if deactivated:
            continue
        out.add(str(npi).zfill(10))
    if not out:
        raise ChatHealthyException(
            mode="nppes_staging_empty",
            message=(
                f"provider_flags_engine: {STAGING_DB_NAME}.{coll_name} "
                f"produced zero non-deactivated NPIs -- staging load failed."
            ),
            collection=coll_name,
        )
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
    """Stamp `status` on doc['taxonomies'][i] ONLY for the 0.001% of
    codes that are not normal NUCC-backed. Absent = normal (default);
    'Supplemented' = code exists in F-105 as an operational supplement
    (also stamps grouping/classification/specialization/definition from
    the F-105 supplement entry); 'Missing' = code is in neither current
    NUCC nor F-105.

    Ceremonial bytes matter across 9.25M records -- normal records
    stay untouched so downstream serialization is unchanged for them.
    See NUCC_SpecialtyCodeDataDiscrepancyManagement.docx."""
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
    registered_npis: set[str],
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
    npi = str(doc.get("npi") or "").zfill(10) if doc.get("npi") else None
    is_npi_registered = bool(npi and npi in registered_npis)
    return {
        "can_prescribe": can_prescribe,
        "is_homeopathic": is_homeopathic,
        "is_npi_registered": is_npi_registered,
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

    catalog = _load_catalog(mongo, data_version)
    registered = _load_registered_npis(mongo, data_version)

    # Discrepancy sink: every unresolved taxonomy code encountered inside
    # this run gets forwarded to report_discrepancy() which persists it
    # to the work_manager entity for the tail-of-run discrepancy report.
    # See LLD v42 sec. 6.9 Data Quality and
    # NUCC_SpecialtyCodeDataDiscrepancyManagement.docx for governance.
    from discrepancy_reporter import report_discrepancy

    def _sink(entry: dict) -> None:
        report_discrepancy(
            load_id=run_id,
            record_key=entry.get("npi"),
            reason=entry.get("reason", "unresolved_taxonomy_code"),
            ctx={
                "code": entry.get("code"),
                "message": entry.get("message"),
                "step": "provider_flags_enrichment",
            },
        )

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
    if partition_state:
        query["addresses"] = {"$elemMatch": {"address_type": "business", "state": partition_state}}

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
    for doc in coll.find(query, projection):
        matched += 1
        flags = _apply_flags_to_doc(
            doc,
            catalog=catalog,
            registered_npis=registered,
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

    return {
        "matched": matched,
        "modified": modified,
        "unresolved_taxonomy_count": unresolved,
        "catalog_size": len(catalog),
        "registered_npi_count": len(registered),
    }
