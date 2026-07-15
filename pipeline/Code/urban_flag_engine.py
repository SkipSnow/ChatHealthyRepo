# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Urban/rural classification — LLD v23 §4.14.

Stamps `urban=true|false` on every enriched practice address on every
provider record in the run, using the U.S. Census 2020_UA_COUNTY workbook
staged into `pipeline_sources_rucc` (LLD §6.4) via percent-urban-population
thresholding.

Note on source name: the staging collection key is `pipeline_sources_rucc`
by legacy convention. The workbook loaded into it for LLD v23 is the
Census 2020_UA_COUNTY workbook. The USDA RUCC codes remain available in
the same staging rows but are NOT what the urban flag is derived from.

Realizes:

  - EPIC-010-F-103-S-005-REQ-B-001  urban flag stamped on every county
                                    identity using Census 2020_UA_COUNTY
  - EPIC-010-F-103-S-005-REQ-B-002  percent-urban-population thresholding

Thresholding contract (LLD §4.14):
  A county is `urban=true` iff the county's percent_urban_population from
  the 2020_UA_COUNTY workbook is greater-than-or-equal-to the configured
  threshold (default 50.0). Counties below the threshold are `urban=false`.
  Counties whose FIPS is missing from the workbook are stamped
  `urban=null` and a `rucc_lookup_miss` discrepancy row is written.

Eligibility: only addresses that already carry a populated `county.fips`
are eligible (i.e., §4.13 succeeded for that address). Mailing addresses
were never eligible for county enrichment, so they are transparently
skipped.

Public entry point: `stamp_urban_flags(config, mongo, blob)`.
"""

from __future__ import annotations

import logging
from typing import Any

_log = logging.getLogger(__name__)

CENSUS_UA_COUNTY_STAGING = "pipeline_sources_rucc"
DEFAULT_URBAN_THRESHOLD_PCT = 50.0
DEFAULT_BATCH = 500

PERCENT_URBAN_KEYS = (
    "percent_urban_population",
    "PCT_URBAN",
    "pct_urban_2020",
    "POPPCT_URBAN",
    "Percent Urban Population",
)


def _first_present(row: dict, keys: tuple[str, ...]) -> str | None:
    for k in keys:
        v = row.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    return None


def _parse_pct(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(str(value).strip().rstrip("%"))
    except (TypeError, ValueError):
        return None


def _load_percent_urban_by_fips(
    mongo, env_prefix: str, run_id: str
) -> dict[str, float]:
    """Load {fips -> percent_urban_population} from the Census 2020_UA_COUNTY workbook."""
    coll = mongo[f"{env_prefix}_PublicHealthData"][CENSUS_UA_COUNTY_STAGING]
    out: dict[str, float] = {}
    for row in coll.find({"run_id": run_id}):
        raw = row.get("raw") or {}
        fips = str(row.get("fips") or raw.get("FIPS") or "").strip().zfill(5)
        if not fips or fips == "00000":
            continue
        pct = _parse_pct(_first_present(raw, PERCENT_URBAN_KEYS))
        if pct is None:
            continue
        out.setdefault(fips, pct)
    return out


def _record_lookup_miss(
    discrepancies_coll,
    *,
    run_id: str,
    npi: str | None,
    fips: str,
) -> None:
    discrepancies_coll.insert_one({
        "run_id": run_id,
        "npi": npi,
        "reason": "rucc_lookup_miss",
        "step": "urban_flag",
        "detail": {"fips": fips},
    })


def stamp_urban_flags(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Stamp `urban` on every enriched practice address in the run.

    Required config keys:
      - run_id                 (str)
      - env                    (str)
      - provider_collection    (str "<db>.<coll>")
      - urban_threshold_pct    (float, default 50.0)
      - batch_size             (int, default 500)
      - partition_state        (str | None) — restrict scan to this state

    Returns:
      {
        "total_addresses":  int,
        "urban_true":       int,
        "urban_false":      int,
        "lookup_miss":      int,
        "providers_updated": int,
      }
    """
    run_id = config["run_id"]
    env_prefix = config["env"]
    provider_collection = config["provider_collection"]
    threshold = float(config.get("urban_threshold_pct", DEFAULT_URBAN_THRESHOLD_PCT))
    partition_state = (config.get("partition_state") or "").upper() or None

    pct_by_fips = _load_percent_urban_by_fips(mongo, env_prefix, run_id)
    if not pct_by_fips:
        _log.warning("urban_flag_engine: no 2020_UA_COUNTY rows for run_id=%s", run_id)

    db_name, coll_name = provider_collection.split(".", 1)
    coll = mongo[db_name][coll_name]
    discrepancies_coll = mongo["chathealthyfrontend"]["pipeline.discrepancies"]

    query: dict[str, Any] = {"run_id": run_id, "addresses.county.fips": {"$exists": True}}
    if partition_state:
        query["addresses"] = {"$elemMatch": {"address_type": "mailing", "state": partition_state}}

    total = 0
    urban_true = 0
    urban_false = 0
    lookup_miss = 0
    providers_updated = 0

    for doc in coll.find(query, {"npi": 1, "addresses": 1}):
        touched = False
        for addr in (doc.get("addresses") or []):
            if not isinstance(addr, dict):
                continue
            c = addr.get("county")
            if not (isinstance(c, dict) and c.get("fips")):
                continue
            fips = str(c["fips"]).strip().zfill(5)
            total += 1
            pct = pct_by_fips.get(fips)
            if pct is None:
                c["urban"] = None
                lookup_miss += 1
                _record_lookup_miss(
                    discrepancies_coll,
                    run_id=run_id,
                    npi=doc.get("npi"),
                    fips=fips,
                )
            else:
                is_urban = pct >= threshold
                c["urban"] = bool(is_urban)
                c["percent_urban_population"] = pct
                if is_urban:
                    urban_true += 1
                else:
                    urban_false += 1
            touched = True
        if touched:
            coll.update_one(
                {"_id": doc["_id"]},
                {"$set": {"addresses": doc["addresses"]}},
            )
            providers_updated += 1

    return {
        "total_addresses": total,
        "urban_true": urban_true,
        "urban_false": urban_false,
        "lookup_miss": lookup_miss,
        "providers_updated": providers_updated,
    }
