# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""ZipCountyCrosswalkLoader — loads US Census ZCTA-to-county relationship data
into PublicHealthData.ZipCountyCrosswalk.

Source: US Census Bureau ZCTA-to-county relationship file (2020 census).
Each ZIP maps to one or more counties. We store all counties with their
area-based ratio (AREALAND_PART / AREALAND_ZCTA5_20), which approximates
HUD's res_ratio. The primary county (highest ratio) is flagged.

Collection schema per document:
  zip          : str   — 5-digit ZIP code
  county_fips  : str   — 5-digit FIPS code of primary county
  county_name  : str   — human-readable county name
  res_ratio    : float — fraction of ZIP area in primary county
  is_split     : bool  — True if any county has ratio < 0.98
  all_counties : list  — [{fips, name, ratio}] for all counties in this ZIP
  loaded_at    : str   — ISO timestamp
"""

import logging
import os
from collections import defaultdict
from datetime import datetime, timezone

import requests
from pymongo import MongoClient, UpdateOne

from blob_client import get_blob_service
from data_fetcher_base import DataFetcherBase

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


_CENSUS_ZCTA_INDEX_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/"
)


class CrosswalkFetcher(DataFetcherBase):
    """US Census ZCTA-to-county relationship file fetcher.

    URL discovered by AI agent (F-102-S-003-REQ-T-006). No fallback constant;
    on discovery failure the constructor raises and the load fails loudly.
    """
    source_name = "census_zcta_county"

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.source_url = ""

    def _resolve_source_url(self) -> str:
        from source_url_discovery import find_latest_data_url
        return find_latest_data_url(
            source_name="census_zcta_county",
            page_url=_CENSUS_ZCTA_INDEX_URL,
            instructions=(
                "Find the URL of the latest US Census ZCTA-to-County "
                "relationship file. Rules: (a) The file is a pipe-delimited "
                "TXT named like `tab20_zcta520_county20_natl.txt` — national-"
                "scope ZCTA-to-county crosswalk. (b) Pick the national-scope "
                "file (`_natl`), NOT a state-level extract. (c) Return only "
                "the absolute URL."
            ),
        )

    def blob_name(self) -> str:
        return "census_zcta_county_2020.txt"

CROSSWALK_COLLECTION = "dev_PublicHealthData.ZipCountyCrosswalk"
SPLIT_THRESHOLD = 0.98


def load_crosswalk(config: dict = None) -> dict:
    """Download Census ZCTA-county file (if changed) and upsert into MongoDB.

    Uses ETag/checksum guard — skips download if file is unchanged.
    Returns summary dict with counts.
    """
    config = config or {}
    collection_name = config.get("crosswalk_collection", CROSSWALK_COLLECTION)
    db_name, coll_name = collection_name.split(".", 1)

    fetcher = CrosswalkFetcher(config)
    fetch_result = fetcher.fetch()

    if fetch_result["skipped"]:
        logging.info(
            "Census crosswalk already landed (blob: %s) — reading from blob.",
            fetch_result["blob_path"],
        )
    else:
        logging.info(
            "Census crosswalk downloaded (blob: %s, sha256: %s…).",
            fetch_result["blob_path"], fetch_result["checksum_sha256"][:16],
        )

    # Always read from blob — never re-download from CMS
    blob_service = get_blob_service()
    container = config.get("blob_container", "provider-data")
    stream = (
        blob_service
        .get_container_client(container)
        .get_blob_client(fetch_result["blob_path"])
        .download_blob()
    )
    raw_bytes = b"".join(stream.chunks())
    logging.info("Parsing Census ZCTA-to-county relationship file from blob...")
    content = raw_bytes.decode("utf-8-sig")
    lines = content.splitlines()

    # Parse pipe-delimited file
    # Columns we need: GEOID_ZCTA5_20 (idx 1), GEOID_COUNTY_20 (idx 9),
    #                  NAMELSAD_COUNTY_20 (idx 10), AREALAND_ZCTA5_20 (idx 3),
    #                  AREALAND_PART (idx 16)
    zip_counties = defaultdict(list)  # zip -> [{fips, name, area_zip, area_part}]

    for line in lines[1:]:
        parts = line.split("|")
        if len(parts) < 17:
            continue
        zip_code = parts[1].strip()
        if not zip_code or len(zip_code) != 5:
            continue

        county_fips = parts[9].strip()
        county_name = parts[10].strip()
        area_zip = int(parts[3]) if parts[3].strip().isdigit() else 0
        area_part = int(parts[16]) if parts[16].strip().isdigit() else 0

        if not county_fips or area_zip == 0:
            continue

        zip_counties[zip_code].append({
            "fips": county_fips,
            "name": county_name,
            "area_zip": area_zip,
            "area_part": area_part,
        })

    logging.info("Parsed %d distinct ZIP codes from Census file", len(zip_counties))

    # Build documents — primary county = highest area_part ratio
    now = datetime.now(timezone.utc).isoformat()
    ops = []

    for zip_code, counties in zip_counties.items():
        area_zip = counties[0]["area_zip"]  # same for all rows of this ZIP

        county_list = []
        for c in counties:
            ratio = round(c["area_part"] / area_zip, 6) if area_zip > 0 else 0.0
            county_list.append({
                "fips": c["fips"],
                "name": c["name"],
                "ratio": ratio,
            })

        # Sort by ratio descending — primary county first
        county_list.sort(key=lambda x: x["ratio"], reverse=True)
        primary = county_list[0]
        is_split = primary["ratio"] < SPLIT_THRESHOLD

        doc = {
            "zip": zip_code,
            "county_fips": primary["fips"],
            "county_name": primary["name"],
            "res_ratio": primary["ratio"],
            "is_split": is_split,
            "all_counties": county_list,
            "loaded_at": now,
        }

        ops.append(UpdateOne({"zip": zip_code}, {"$set": doc}, upsert=True))

    # Batch upsert
    collection = _get_mongo_client()[db_name][coll_name]
    collection.create_index("zip", unique=True)

    batch_size = 1000
    upserted = 0
    for i in range(0, len(ops), batch_size):
        result = collection.bulk_write(ops[i:i + batch_size], ordered=False)
        upserted += result.upserted_count + result.modified_count

    total = collection.count_documents({})
    split_count = collection.count_documents({"is_split": True})
    logging.info(
        "Crosswalk loaded: %d total ZIPs, %d split (ratio < %.0f%%)",
        total, split_count, SPLIT_THRESHOLD * 100,
    )
    return {
        "total_zips": total,
        "split_zips": split_count,
        "single_county_zips": total - split_count,
    }


if __name__ == "__main__":
    import sys
    from pathlib import Path
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent / ".env")
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = load_crosswalk()
    print(result)
