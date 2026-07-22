# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Proprietary provider-flag enrichment — LLD v23 §4.15 / §4.16 + F-105.

Applies the four F-105 flags to every provider record in the run based on
the provider's primary taxonomy code and (for is_npi_registered) the
current NPPES row of record:

  can_prescribe        (bool)  — from F-105 catalog by NUCC code
  is_homeopathic       (bool)  — from F-105 catalog by NUCC code
  is_disqualified      (bool)  — from F-105 catalog by NUCC code
                                 (NUCC codes that do not represent
                                  licensed clinical roles)
  is_npi_registered    (bool)  — true iff the NPI appears in the NPPES
                                 staging load and is not deactivated

Realizes:

  - EPIC-010-F-103-S-007-REQ-B-001..REQ-B-004  proprietary flag enrichment
                                               substrate on provider records
  - EPIC-010-F-105-S-001-REQ-B-001, REQ-B-002  single source of truth for
                                               classification (F-105 catalog)
  - EPIC-010-F-105-S-002-REQ-B-001             can_prescribe per NUCC code
  - EPIC-010-F-105-S-003-REQ-B-001             is_homeopathic per NUCC code

The F-105 catalog is materialized into the pipeline-cluster staging
collection `pipeline_sources_specialty_catalog` per LLD §4.7. This
module treats that collection as read-only.

Non-goal: this module does NOT set the F-105 flags on
SpecialtyMetaData — that path is the Specialty Pipeline (F-104-S-004),
sibling to this one.

Public entry point: `apply_provider_flags(config, mongo, blob)`.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from typing import Any

from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

CATALOG_COLLECTION = "pipeline_sources_specialty_catalog"
NPPES_STAGING_COLLECTION = "pipeline_sources_nppes_npi"
NPPES_DEACTIVATION_KEYS = (
    "NPI Deactivation Date",
    "npi_deactivation_date",
    "Provider Deactivation Date",
)
DEFAULT_BATCH = 500


def _load_catalog(mongo, env_prefix: str, run_id: str) -> dict[str, dict[str, bool]]:
    """Load F-105 catalog rows into {code -> flags}."""
    coll = mongo[f"{env_prefix}_PublicHealthData"][CATALOG_COLLECTION]
    q = {"run_id": run_id}
    total = coll.count_documents(q)
    out: dict[str, dict[str, bool]] = {}
    cursor = coll.find(q) if total > 0 else coll.find({})
    for row in cursor:
        raw = row.get("raw") or row
        code = str(raw.get("code") or row.get("code") or "").strip()
        if not code:
            continue
        out[code] = {
            "can_prescribe": bool(raw.get("can_prescribe", False)),
            "is_homeopathic": bool(raw.get("is_homeopathic", False)),
            "is_disqualified": not bool(raw.get("is_licensed_clinical_role", True)),
        }
    return out


def _load_registered_npis(mongo, env_prefix: str, run_id: str) -> set[str]:
    """Every NPI in the NPPES staging load that is not deactivated."""
    coll = mongo[f"{env_prefix}_PublicHealthData"][NPPES_STAGING_COLLECTION]
    out: set[str] = set()
    for row in coll.find({"run_id": run_id}, {"npi": 1, "raw": 1}):
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
    return out


def _primary_taxonomy_code(doc: dict) -> str | None:
    """Return the primary taxonomy code from the provider record."""
    for tax in (doc.get("taxonomies") or []):
        if isinstance(tax, dict) and tax.get("primary") is True:
            code = tax.get("code")
            if code:
                return str(code).strip()
    tax_list = doc.get("taxonomies") or []
    if tax_list and isinstance(tax_list[0], dict) and tax_list[0].get("code"):
        return str(tax_list[0]["code"]).strip()
    return None


def _apply_flags_to_doc(
    doc: dict,
    *,
    catalog: dict[str, dict[str, bool]],
    registered_npis: set[str],
) -> dict[str, bool]:
    code = _primary_taxonomy_code(doc)
    flags: dict[str, bool] = {
        "can_prescribe": False,
        "is_homeopathic": False,
        "is_disqualified": True,
        "is_npi_registered": False,
    }
    if code and code in catalog:
        cat = catalog[code]
        flags["can_prescribe"] = bool(cat.get("can_prescribe", False))
        flags["is_homeopathic"] = bool(cat.get("is_homeopathic", False))
        flags["is_disqualified"] = bool(cat.get("is_disqualified", False))
    npi = str(doc.get("npi") or "").zfill(10) if doc.get("npi") else None
    if npi and npi in registered_npis:
        flags["is_npi_registered"] = True
    return flags


def apply_provider_flags(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Stamp the four F-105 flags on every provider record in the run.

    Required config keys:
      - run_id                (str)
      - env                   (str)
      - provider_collection   (str "<db>.<coll>")
      - entity_kind_filter    (str | None) — "individual" | "institutional"
      - partition_state       (str | None) — restrict to this business state
      - batch_size            (int, default 500)

    Returns:
      {
        "matched":              int,
        "modified":              int,
        "catalog_size":          int,
        "registered_npi_count":  int,
        "missing_catalog_codes": int,
      }
    """
    run_id = config["run_id"]
    env_prefix = config["env"]
    provider_collection = config["provider_collection"]
    entity_kind_filter = config.get("entity_kind_filter")
    partition_state = (config.get("partition_state") or "").upper() or None
    batch_size = int(config.get("batch_size", DEFAULT_BATCH))

    catalog = _load_catalog(mongo, env_prefix, run_id)
    registered = _load_registered_npis(mongo, env_prefix, run_id)

    db_name, coll_name = provider_collection.split(".", 1)
    coll = mongo[db_name][coll_name]

    query: dict[str, Any] = {"run_id": run_id}
    if entity_kind_filter == "individual":
        query["entity_type_code"] = "1"
    elif entity_kind_filter == "institutional":
        query["entity_type_code"] = "2"
    if partition_state:
        query["addresses"] = {"$elemMatch": {"address_type": "mailing", "state": partition_state}}

    ops: list[UpdateOne] = []
    modified = 0
    matched = 0
    missing_codes = 0

    for doc in coll.find(query, {"npi": 1, "taxonomies": 1}):
        matched += 1
        flags = _apply_flags_to_doc(
            doc, catalog=catalog, registered_npis=registered
        )
        code = _primary_taxonomy_code(doc)
        if code and code not in catalog:
            missing_codes += 1
        ops.append(UpdateOne({"_id": doc["_id"]}, {"$set": flags}))
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
        "catalog_size": len(catalog),
        "registered_npi_count": len(registered),
        "missing_catalog_codes": missing_codes,
    }
