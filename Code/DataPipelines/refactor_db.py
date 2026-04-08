# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).

"""refactor_db.py — server-side copy of PublicHealthData → dev_PublicHealthData
on the DataPipelines cluster.

providers_staging is renamed to providers during the copy.
providers_enriched, SpecialtyMetaData, ICD10Codes, ZipCountyCrosswalk keep their names.
ZipCountyCrosswalk is skipped if already present in dev_PublicHealthData.

Does NOT drop PublicHealthData — run drop manually after smoke test passes.

Usage:
    python refactor_db.py [--dry-run]
"""

import argparse
import logging
import os
import sys
import winsound
from pathlib import Path


def bell(success: bool = True):
    """Play a short alert tone. Three beeps = success, one low beep = failure."""
    if success:
        for _ in range(3):
            winsound.Beep(1000, 200)
    else:
        winsound.Beep(400, 800)

from dotenv import load_dotenv
from pymongo import MongoClient

_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger("refactor_db")

# (src_collection, dst_collection)
COPIES = [
    ("providers_staging", "providers"),       # rename during copy
    ("providers_enriched", "providers_enriched"),
    ("SpecialtyMetaData",  "SpecialtyMetaData"),
    ("ICD10Codes",         "ICD10Codes"),
    ("ZipCountyCrosswalk", "ZipCountyCrosswalk"),
]

SRC_DB  = "PublicHealthData"
DST_DB  = f"{os.environ.get('ENV_PREFIX', 'dev')}_PublicHealthData"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = os.environ.get("MONGO_connectionString")
    if not conn:
        log.error("MONGO_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=30_000)
    src_db = client[SRC_DB]
    dst_db = client[DST_DB]

    existing_src = src_db.list_collection_names()
    existing_dst = dst_db.list_collection_names()
    log.info("Source (%s) collections: %s", SRC_DB, existing_src)
    log.info("Destination (%s) collections: %s", DST_DB, existing_dst)

    for src_coll, dst_coll in COPIES:
        if src_coll not in existing_src:
            log.warning("  SKIP %s — not found in %s", src_coll, SRC_DB)
            continue

        src_count = src_db[src_coll].count_documents({})
        dst_count = dst_db[dst_coll].count_documents({}) if dst_coll in existing_dst else 0

        if dst_count >= src_count > 0:
            log.info("  SKIP %s → %s — dst already has %d docs", src_coll, dst_coll, dst_count)
            continue

        label = f"{src_coll} → {dst_coll}" if src_coll != dst_coll else src_coll
        log.info("  COPY %s.%s → %s.%s  (%d docs)", SRC_DB, src_coll, DST_DB, dst_coll, src_count)

        if args.dry_run:
            continue

        list(src_db[src_coll].aggregate(
            [{"$out": {"db": DST_DB, "coll": dst_coll}}],
            allowDiskUse=True,
        ))

        copied = dst_db[dst_coll].count_documents({})
        log.info("  DONE %s — %d docs in %s", label, copied, DST_DB)

    log.info("=== Final counts in %s ===", DST_DB)
    for _, dst_coll in COPIES:
        count = dst_db[dst_coll].count_documents({})
        log.info("  %s: %d", dst_coll, count)

    client.close()
    log.info("Done. PublicHealthData NOT dropped — drop manually after smoke test.")
    bell(success=True)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        bell(success=False)
        raise
