from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_frontend_lib.exceptions import ChatHealthyException
# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""ICD-10-CM Loader — downloads CMS code descriptions and upserts into MongoDB.

Source: CMS ICD-10-CM Code Descriptions in Tabular Order (annual + mid-year updates).
Uses DataFetcherBase ETag guard — skips download if file is unchanged.

Collection schema per document (PublicHealthData.ICD10Codes):
  code         : str   — ICD-10-CM code (e.g. "A00.0")
  description  : str   — full description
  short_desc   : str   — abbreviated description (if available)
  is_header    : bool  — True if this is a category header (not billable)
  loaded_at    : str   — ISO timestamp
  version      : str   — source file version/signature

Poll frequency: check monthly — CMS releases mid-year updates April 1.
The DataSourceRegistry record stores poll_frequency = "monthly".
"""

import io

import os
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient, UpdateOne

from blob_client import get_blob_service
from data_fetcher_base import DataFetcherBase

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = ChatHealthyMongoUtilities("MONGO_connectionString").getConnection()
    return _mongo

_ENV_PREFIX = os.environ.get("ENV_PREFIX", "dev")
ICD10_COLLECTION = f"{_ENV_PREFIX}_PublicHealthData.ICD10Codes"

# CMS releases ICD-10-CM annually (Oct 1) with a mid-year update (Apr 1).
# The scraper below checks the CMS page for the latest URL.
# Fallback to last-known URL if scraping fails.
ICD10_BASE_URL = "https://www.cms.gov"
ICD10_FALLBACK_URL = (
    "https://www.cms.gov/files/zip/april-1-2026-code-descriptions-tabular-order.zip"
)
ICD10_INDEX_URL = "https://www.cms.gov/medicare/coding-billing/icd-10-codes"


def _discover_icd10_url() -> str:
    """Scrape CMS ICD-10 page for the most recent code descriptions zip."""
    import re
    import requests
    resp = requests.get(ICD10_INDEX_URL, timeout=30)
    resp.raise_for_status()
    # Find all tabular order zips — pick the most recent (highest year, prefer April update)
    matches = re.findall(
        r'"/files/zip/([^"]*code-descriptions-tabular-order[^"]*\.zip)"',
        resp.text,
        re.IGNORECASE,
    )
    if not matches:
        raise ChatHealthyException(mode="runtime_error", message="Could not find ICD-10-CM tabular order zip on CMS page.")
    # First match is most recent (CMS lists newest first)
    path = matches[0]
    url = f"{ICD10_BASE_URL}/files/zip/{path}"
    return url


class Icd10Fetcher(DataFetcherBase):
    """ICD-10-CM code descriptions fetcher. Polls CMS monthly."""
    source_name = "icd10_cm"

    def __init__(self, config: dict = None):
        super().__init__(config)
        # No fallback per operator directive: silent fallback to a stale
        # hardcoded April-2026 URL is worse than failing loudly. If
        # discovery breaks, the pipeline MUST surface the problem, not
        # silently reprocess months-old data.
        self.source_url = _discover_icd10_url()

    def blob_name(self) -> str:
        # Derive a short name from the URL
        return "icd10_cm_latest.zip"

    @property
    def poll_frequency(self) -> str:
        return "monthly"


def _parse_icd10_zip(zip_bytes: bytes) -> list[dict]:
    """Parse ICD-10-CM tabular order zip. Returns list of code dicts.

    CMS zip contains two files:
      - icd10cm_order_YYYY.txt  — fixed-width, full + abbreviated descriptions
      - icd10cm_tabular_YYYY.xml — XML tabular (we use the flat order file)

    Fixed-width format (icd10cm_order_*.txt):
      Col 1-5   : order number (sequence, not the code)
      Col 6     : space
      Col 7-13  : ICD-10-CM code (left-justified, space-padded, no dots)
      Col 14    : space
      Col 15    : valid_for_coding (1=billable, 0=header)
      Col 16    : space
      Col 17-76 : short description
      Col 77+   : long description
    """
    codes = []
    now = datetime.now(timezone.utc).isoformat()

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        # Find the order file
        order_file = next(
            (n for n in zf.namelist()
             if "order" in n.lower() and n.endswith(".txt")),
            None,
        )
        if not order_file:
            raise ChatHealthyException(mode="runtime_error", message=f"No order .txt file found in ICD-10-CM zip. Files: {zf.namelist()}")

        with zf.open(order_file) as f:
            for line in f:
                line = line.decode("utf-8", errors="replace").rstrip("\n")
                if len(line) < 16:
                    continue

                raw_code = line[6:13].strip()
                if not raw_code:
                    continue

                # Insert dot at position 3 for display (e.g. A000 → A00.0)
                if len(raw_code) > 3:
                    code = raw_code[:3] + "." + raw_code[3:]
                else:
                    code = raw_code

                is_billable = line[14:15].strip() == "1"
                short_desc = line[16:76].strip() if len(line) > 76 else line[16:].strip()
                long_desc = line[76:].strip() if len(line) > 76 else ""

                codes.append({
                    "code": code,
                    "description": long_desc or short_desc,
                    "short_desc": short_desc,
                    "is_header": not is_billable,
                    "loaded_at": now,
                })

    return codes


def load_icd10(config: dict = None) -> dict:
    """Fetch ICD-10-CM file (if changed) and upsert into MongoDB.

    Returns summary dict with counts.
    """
    config = config or {}
    collection_name = config.get("icd10_collection", ICD10_COLLECTION)
    db_name, coll_name = collection_name.split(".", 1)

    fetcher = Icd10Fetcher(config)

    # Register poll_frequency in the registry (stored alongside checksum)
    config["_poll_frequency"] = fetcher.poll_frequency

    fetch_result = fetcher.fetch()

    if fetch_result["skipped"]:
        ChatHealthyLoggingService().info(
            "ICD-10-CM already landed (blob: %s) — no update needed.",
            fetch_result["blob_path"],
        )
        return {
            "skipped": True,
            "version": fetch_result["version"],
            "blob_path": fetch_result["blob_path"],
        }

    ChatHealthyLoggingService().info(
        "ICD-10-CM downloaded (blob: %s, sha256: %s…) — loading into MongoDB.",
        fetch_result["blob_path"], fetch_result["checksum_sha256"][:16],
    )

    # Read from blob
    container = config.get("blob_container", "provider-data")
    blob_service = get_blob_service()
    # v4-001D: stream blob in chunks instead of loading entire blob at once
    blob_stream = (
        blob_service
        .get_container_client(container)
        .get_blob_client(fetch_result["blob_path"])
        .download_blob()
    )
    chunks = []
    for chunk in blob_stream.chunks():
        chunks.append(chunk)
    zip_bytes = b"".join(chunks)

    codes = _parse_icd10_zip(zip_bytes)

    # Upsert into MongoDB
    collection = _get_mongo_client()[db_name][coll_name]
    collection.create_index("code", unique=True)

    batch_size = 1000
    upserted = 0
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        ops = [
            UpdateOne({"code": c["code"]}, {"$set": c}, upsert=True)
            for c in batch
        ]
        result = collection.bulk_write(ops, ordered=False)
        upserted += result.upserted_count + result.modified_count

    total = collection.count_documents({})
    billable = collection.count_documents({"is_header": False})
    ChatHealthyLoggingService().info(
        "ICD-10-CM loaded: %d total codes (%d billable, %d headers)",
        total, billable, total - billable,
    )
    return {
        "skipped": False,
        "version": fetch_result["version"],
        "total_codes": total,
        "billable_codes": billable,
        "header_codes": total - billable,
        "upserted": upserted,
    }


if __name__ == "__main__":
    load_dotenv(Path(__file__).parent.parent / ".env")
    result = load_icd10()
    import json
    ChatHealthyLoggingService().info(json.dumps(result, indent=2))
