"""Urban-flag stamping stage for the FindCare pipeline.

Reads the U.S. Census Bureau 2020 county urban-rural tabulation
(2020_UA_COUNTY.xlsx) from blob storage (or downloads from the Census
URL if absent in blob and caches it back), builds a (county_fips -> urban_bool)
lookup, then writes `urban: <bool>` into every practice_address[i].county
that already has a non-null fips.

Threshold: a county is urban when POPPCT_URB >= 50.0 (majority of population
in 2020 Census urban areas), else rural.

This stage runs AFTER county enrichment (Pass 1-6 have populated county.fips)
and BEFORE embedding generation. It is a separate stage of the FindCare
pipeline (not bundled into county_enrichment_job).
"""

import io
import logging
import os
import tempfile
from typing import Iterable

import requests

CENSUS_URL = "https://www2.census.gov/geo/docs/reference/ua/2020_UA_COUNTY.xlsx"
CENSUS_BLOB_NAME = "census_2020_ua_county.xlsx"
# Census file stores POPPCT_URB / POPPCT_RUR as fractions (0..1), not percentages.
# Threshold 0.5 means majority-urban county.
URBAN_THRESHOLD = 0.5

_log = logging.getLogger("urban_flag")


def _load_xlsx_from_blob_or_census(blob_container: str) -> bytes:
    """Return the raw bytes of the Census 2020_UA_COUNTY.xlsx file.

    Tries blob storage first; if absent, downloads from Census and uploads
    to blob for the next run."""
    from blob_client import get_blob_service

    svc = get_blob_service()
    cc = svc.get_container_client(blob_container)
    blob = cc.get_blob_client(CENSUS_BLOB_NAME)

    try:
        data = blob.download_blob().readall()
        _log.info("urban_flag: loaded Census file from blob (%.2f MB)", len(data) / 1e6)
        return data
    except Exception as exc:
        _log.info("urban_flag: blob miss (%s) — fetching from Census", exc)

    resp = requests.get(CENSUS_URL, timeout=120)
    resp.raise_for_status()
    data = resp.content
    _log.info("urban_flag: downloaded from Census (%.2f MB)", len(data) / 1e6)

    try:
        blob.upload_blob(data, overwrite=True)
        _log.info("urban_flag: cached to blob %s", CENSUS_BLOB_NAME)
    except Exception as exc:
        _log.warning("urban_flag: could not cache to blob (%s)", exc)
    return data


def _parse_county_urban(xlsx_bytes: bytes) -> dict[str, bool]:
    """Parse the 2020_UA_COUNTY workbook into {county_fips_5digit -> urban_bool}.

    Reads BOTH the main '2020_UA_COUNTY' sheet (legacy CT counties + 49 other
    states + DC + PR) and 'CT_2022' (Connecticut planning regions). Connecticut
    rows from the main sheet are kept alongside the planning-region rows so
    legacy and post-2022 county FIPS both resolve correctly."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(xlsx_bytes), data_only=True, read_only=True)
    out: dict[str, bool] = {}

    for sheet_name in wb.sheetnames:
        if sheet_name not in ("2020_UA_COUNTY", "CT_2022"):
            continue
        ws = wb[sheet_name]
        rows = ws.iter_rows(values_only=True)
        header = next(rows, None)
        if header is None:
            continue
        try:
            i_state = header.index("STATE")
            i_county = header.index("COUNTY")
            i_pct_urb = header.index("POPPCT_URB")
        except ValueError:
            _log.warning("urban_flag: sheet %s missing expected columns: %s", sheet_name, header)
            continue

        for row in rows:
            if row is None or len(row) <= max(i_state, i_county, i_pct_urb):
                continue
            state_raw = row[i_state]
            county_raw = row[i_county]
            pct_raw = row[i_pct_urb]
            if state_raw is None or county_raw is None or pct_raw is None:
                continue
            try:
                state = str(state_raw).strip().zfill(2)
                county = str(county_raw).strip().zfill(3)
                pct = float(pct_raw)
            except (TypeError, ValueError):
                continue
            fips = state + county
            if len(fips) != 5:
                continue
            out[fips] = pct >= URBAN_THRESHOLD

    _log.info(
        "urban_flag: parsed %d county FIPS from Census (urban=%d, rural=%d, threshold=%.2f)",
        len(out),
        sum(1 for v in out.values() if v),
        sum(1 for v in out.values() if not v),
        URBAN_THRESHOLD,
    )
    return out


def _distinct_county_fips_in_use(coll, state_filter: dict) -> Iterable[str]:
    """Yield every distinct county.fips currently set on any practice_address
    element of any in-scope provider. Drives the per-FIPS update loop."""
    pipeline = [
        {"$match": state_filter},
        {"$unwind": {"path": "$practice_address", "preserveNullAndEmptyArrays": False}},
        {"$match": {"practice_address.county.fips": {"$type": "string"}}},
        {"$group": {"_id": "$practice_address.county.fips"}},
    ]
    for doc in coll.aggregate(pipeline, allowDiskUse=True):
        fips = doc.get("_id")
        if fips:
            yield fips


def stamp_urban_flag_fn(config: dict) -> dict:
    """Pipeline stage: stamp practice_address[i].county.urban from Census data.

    Idempotent. Safe to re-run. Per-FIPS updateMany with arrayFilters touches
    every matching element in a single Mongo round-trip per FIPS.
    Returns counts for the orchestrator to log/report.
    """
    from pymongo import MongoClient
    from state_filter import normalize_states, mongo_state_filter, is_full_load

    blob_container = config.get("blob_container", "provider-data")
    provider_collection = config.get(
        "provider_collection", "dev_PublicHealthData.providers"
    )
    db_name, coll_name = provider_collection.split(".", 1)
    coll = MongoClient(
        os.environ["MONGO_connectionString"],
        serverSelectionTimeoutMS=120_000,
        socketTimeoutMS=900_000,
    )[db_name][coll_name]

    states = normalize_states(config)
    state_filter = {} if is_full_load(states) else mongo_state_filter(states)

    xlsx_bytes = _load_xlsx_from_blob_or_census(blob_container)
    fips_to_urban = _parse_county_urban(xlsx_bytes)

    # Drive updates by the FIPS values actually present in our collection (so
    # we don't issue thousands of updateMany calls for FIPS no provider has).
    in_use = list(_distinct_county_fips_in_use(coll, state_filter))
    _log.info("urban_flag: %d distinct county FIPS in scope", len(in_use))

    elements_updated = 0
    fips_unmatched = 0
    for fips in in_use:
        urban = fips_to_urban.get(fips)
        if urban is None:
            fips_unmatched += 1
            continue
        result = coll.update_many(
            {**state_filter,
             "practice_address": {"$elemMatch": {"county.fips": fips}}},
            {"$set": {"practice_address.$[elem].county.urban": urban}},
            array_filters=[{"elem.county.fips": fips}],
        )
        elements_updated += result.modified_count

    summary = {
        "fips_in_scope": len(in_use),
        "fips_unmatched_in_census": fips_unmatched,
        "providers_modified": elements_updated,
        "urban_threshold_pct": URBAN_THRESHOLD,
    }
    _log.info("urban_flag: %s", summary)
    return summary
