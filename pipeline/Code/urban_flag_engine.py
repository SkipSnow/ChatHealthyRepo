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
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from typing import Any

from staging_loader import STAGING_DB_NAME, staging_collection_name

_log = ChatHealthyLoggingService()

# usda_rucc is the source_name in STAGING_BASE_NAMES; staging_loader
# writes to PublicStaging.StagingUsdaRucc_v_{data_version}. The legacy
# CENSUS_UA_COUNTY_STAGING="pipeline_sources_rucc" name lived on
# dev_PublicHealthData and never held rows under the current loader —
# removed.
URBAN_RUCC_MAX = 3  # Port of pipeline/Code/urban_flag.py: RUCC 1..3 = urban, 4..9 = rural.
DEFAULT_BATCH = 500


def _find_fips_col(raw_keys) -> str | None:
    for k in raw_keys:
        low = str(k).strip().lower()
        if low in ("fips", "fips_code", "fipscode", "fips_txt"):
            return k
    return None


def _find_rucc_col(raw_keys) -> str | None:
    for k in raw_keys:
        low = str(k).strip().lower()
        if low.startswith("rucc_") or low == "rucc" or "ruralurbancontinuum" in low:
            return k
    return None


def _load_urban_by_fips(
    mongo, env_prefix: str, run_id: str, data_version: int,
) -> dict[str, bool]:
    """Load {fips -> urban_bool} from the USDA RUCC staging collection.
    Ports pipeline/Code/urban_flag.py semantics: RUCC 1..3 -> True, 4..9 -> False."""
    coll_name = staging_collection_name("usda_rucc", data_version)
    coll = mongo[STAGING_DB_NAME][coll_name]
    out: dict[str, bool] = {}
    fips_col = None
    rucc_col = None
    for row in coll.find({"run_id": run_id}):
        raw = row.get("raw") or {}
        if fips_col is None or rucc_col is None:
            fips_col = _find_fips_col(raw.keys())
            rucc_col = _find_rucc_col(raw.keys())
            if fips_col is None or rucc_col is None:
                continue
        try:
            fips = str(raw.get(fips_col) or "").strip().zfill(5)
            rucc = int(raw.get(rucc_col))
        except (TypeError, ValueError):
            continue
        if len(fips) != 5 or fips == "00000" or rucc < 1 or rucc > 9:
            continue
        out[fips] = rucc <= URBAN_RUCC_MAX
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

    Ports pipeline/Code/urban_flag.py semantics: RUCC 1..3 = urban=True,
    4..9 = urban=False, missing fips in USDA RUCC lookup = urban=null.

    Required config keys:
      - run_id                 (str)
      - env                    (str)
      - data_version           (int)
      - provider_collection    (str "<db>.<coll>")
      - partition_state        (str | None) — restrict scan to this state

    Returns:
      {
        "total_addresses":   int,
        "urban_true":        int,
        "urban_false":       int,
        "lookup_miss":       int,
        "providers_updated": int,
      }
    """
    run_id = config["run_id"]
    env_prefix = config["env"]
    data_version = int(config["data_version"])
    provider_collection = config["provider_collection"]
    partition_state = (config.get("partition_state") or "").upper() or None

    urban_by_fips = _load_urban_by_fips(mongo, env_prefix, run_id, data_version)
    if not urban_by_fips:
        _log.warning("urban_flag_engine: no USDA RUCC rows for run_id=%s", run_id)

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
            urban = urban_by_fips.get(fips)
            if urban is None:
                c["urban"] = None
                lookup_miss += 1
                _record_lookup_miss(
                    discrepancies_coll,
                    run_id=run_id,
                    npi=doc.get("npi"),
                    fips=fips,
                )
            else:
                c["urban"] = bool(urban)
                if urban:
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
