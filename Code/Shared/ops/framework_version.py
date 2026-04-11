# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Read or set the framework version in admin.framework_version.
# The framework version tracks architectural maturity (0.1, 0.2, 0.3, etc.)
# and is stored globally in the admin database — not per-environment.
#
# Each version is its own document with from/to dates.
# Active version: to == null (only one at a time).
#
# Usage:
#   python framework_version.py                          # list all versions
#   python framework_version.py --set 0.2 --name "Modular" --note "Refactored main.py into 5 modules"
#   python framework_version.py --set 0.2 --name "Modular" --note "..." --from-date 2026-03-28

import argparse
import logging
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
from pymongo import MongoClient

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("framework_version")
def get_all(client):
    return list(client["admin"]["framework_version"].find().sort("version", 1))


def set_version(client, version: str, name: str, note: str, from_date: str = None):
    coll = client["admin"]["framework_version"]
    now = datetime.now(timezone.utc).isoformat()
    from_val = from_date or now

    # Close out the current active version
    coll.update_one({"to": None}, {"$set": {"to": from_val}})

    # Insert the new version as active (to == null)
    coll.update_one(
        {"_id": version},
        {"$set": {
            "version": version,
            "name": name,
            "note": note,
            "from": from_val,
            "to": None,
        }},
        upsert=True,
    )
    log.info("Framework version set to %s (%s): %s", version, name, note)


def main():
    parser = argparse.ArgumentParser(description="Read or set framework version")
    parser.add_argument("--set", dest="version", help="New version (e.g., 0.2)")
    parser.add_argument("--name", help="Version name (e.g., Modular)")
    parser.add_argument("--note", help="What is new in this version")
    parser.add_argument("--from-date", help="Override start date (ISO format, e.g., 2026-03-28)")
    args = parser.parse_args()

    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = MongoClient(conn, serverSelectionTimeoutMS=10000)

    if args.version:
        if not args.name:
            log.error("--name is required when setting a version")
            sys.exit(1)
        if not args.note:
            log.error("--note is required when setting a version")
            sys.exit(1)
        set_version(client, args.version, args.name, args.note, args.from_date)
    else:
        versions = get_all(client)
        if versions:
            for v in versions:
                active = " ← ACTIVE" if v.get("to") is None else ""
                to_str = v.get("to", "")[:10] if v.get("to") else "present"
                from_str = v.get("from", "")[:10] if v.get("from") else "?"
                log.info("  %s — %s (%s → %s): %s%s",
                         v["version"], v.get("name", ""), from_str, to_str, v.get("note", ""), active)
        else:
            log.info("No framework versions found. Use --set to initialize.")


if __name__ == "__main__":
    main()
