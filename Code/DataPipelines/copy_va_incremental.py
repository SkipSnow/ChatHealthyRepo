# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

# Incremental copy of remaining VA providers from pipeline to frontend.
# Skips records that already exist on frontend (by NPI).
# Low stress: 2 workers, batch size 200.

import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("copy_va")

BATCH_SIZE = 200
WORKERS = 2


def get_missing_npis():
    """Find VA NPIs on pipeline that aren't on frontend."""
    src = MongoClient(os.environ["MONGO_connectionString"], serverSelectionTimeoutMS=60000)
    dst = MongoClient(os.environ["MONGO_FRONTEND_connectionString"], serverSelectionTimeoutMS=60000)

    src_col = src["dev_PublicHealthData"]["providers"]
    dst_col = dst["dev_PublicHealthData"]["providers"]

    log.info("Finding missing VA NPIs...")
    src_npis = set()
    for doc in src_col.find({"practice_address.state": "VA"}, {"npi": 1}).batch_size(10000):
        src_npis.add(doc.get("npi"))

    dst_npis = set()
    for doc in dst_col.find({"practice_address.state": "VA"}, {"npi": 1}).batch_size(10000):
        dst_npis.add(doc.get("npi"))

    missing = src_npis - dst_npis
    log.info("Source: %d, Frontend: %d, Missing: %d", len(src_npis), len(dst_npis), len(missing))
    return list(missing)


def copy_batch(npis):
    """Copy a batch of NPIs from pipeline to frontend."""
    src = MongoClient(os.environ["MONGO_connectionString"], serverSelectionTimeoutMS=60000)
    dst = MongoClient(os.environ["MONGO_FRONTEND_connectionString"], serverSelectionTimeoutMS=60000)

    src_col = src["dev_PublicHealthData"]["providers"]
    dst_col = dst["dev_PublicHealthData"]["providers"]

    docs = list(src_col.find({"npi": {"$in": npis}}))
    if docs:
        try:
            dst_col.insert_many(docs, ordered=False)
        except Exception:
            pass  # duplicates OK
    return len(docs)


def main():
    missing = get_missing_npis()
    if not missing:
        log.info("Nothing to copy.")
        return

    # Split into batches
    batches = [missing[i:i + BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]
    log.info("Copying %d records in %d batches with %d workers", len(missing), len(batches), WORKERS)

    copied = 0
    start = time.time()

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        for result in pool.map(copy_batch, batches):
            copied += result
            if copied % 2000 == 0:
                elapsed = time.time() - start
                log.info("  %d / %d copied (%.0f/s)", copied, len(missing), copied / elapsed)

    elapsed = time.time() - start
    log.info("COMPLETE: %d copied in %.1f minutes", copied, elapsed / 60)


if __name__ == "__main__":
    main()
