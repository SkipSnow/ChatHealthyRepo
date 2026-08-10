# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Promote provider data from one environment to another on the frontend cluster.
# Copies PublicHealthData (providers + SpecialtyMetaData) and System (build counter).
# Does NOT copy user data (AboutUs, Safety, Debug).
# Usage:
#   python promote_data.py --from dev --to qa
#   python promote_data.py --from dev --to qa --dry-run

import argparse

import os
import sys
import time

from dotenv import load_dotenv
from pymongo import MongoClient

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "FrontEndApplicationLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


log = ChatHealthyLoggingService()

BATCH_SIZE = 5000

# Collections to promote (source_collection, destination_collection)
PROMOTE_COLLECTIONS = [
    ("PublicHealthData", "providers"),
    ("PublicHealthData", "SpecialtyMetaData"),
]



def _devops_connection():
    """The DevOps identity, by certificate. Rule-004: no MongoClient here.

    Operator tooling authenticates as DevOpsUser like everything else in the
    devops chain. It used to open a MongoClient on a SCRAM connection string,
    which is a fourth credential outside the three-identity model and outside
    Rule-004's scan scope, so nothing caught it.
    """
    import sys as _sys, pathlib as _pl
    _src = _pl.Path(__file__).resolve()
    for _p in _src.parents:
        if (_p / ".git").exists():
            _lib = _p / "FrontEndApplicationLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            from dotenv import load_dotenv as _ld
            _ld(_p / ".env", override=False)
            break
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", "admin")


def promote_data(from_env: str, to_env: str, dry_run: bool = False):
    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        log.error("MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    client = _devops_connection()

    for db_suffix, coll_name in PROMOTE_COLLECTIONS:
        src_db = f"{from_env}_{db_suffix}"
        dst_db = f"{to_env}_{db_suffix}"
        src_col = client[src_db][coll_name]
        dst_col = client[dst_db][coll_name]

        src_count = src_col.estimated_document_count()
        dst_count = dst_col.estimated_document_count()

        log.info("%s.%s: %s docs -> %s.%s: %s docs",
                 src_db, coll_name, f"{src_count:,}",
                 dst_db, coll_name, f"{dst_count:,}")

        if dry_run:
            log.info("  DRY RUN — skipping")
            continue

        if dst_count > 0:
            log.info("  Dropping destination...")
            dst_col.drop()

        log.info("  Copying %s docs...", f"{src_count:,}")
        cursor = src_col.find({}, batch_size=BATCH_SIZE, no_cursor_timeout=True)
        batch = []
        copied = 0
        start = time.time()

        try:
            for doc in cursor:
                batch.append(doc)
                if len(batch) >= BATCH_SIZE:
                    dst_col.insert_many(batch, ordered=False)
                    copied += len(batch)
                    elapsed = time.time() - start
                    rate = copied / elapsed if elapsed > 0 else 0
                    log.info("  %s / %s (%.0f doc/s)", f"{copied:,}", f"{src_count:,}", rate)
                    batch = []
            if batch:
                dst_col.insert_many(batch, ordered=False)
                copied += len(batch)
        finally:
            cursor.close()

        elapsed = time.time() - start
        log.info("  Done: %s docs in %.1f s", f"{copied:,}", elapsed)

    # Per BUG-001: build is global, in frontEndAdmin.BuildVersions, bumped at build time
    # (see DeploymentArchitectureDesignAndMigrationPlanPhase_v14.docx section 2.1 build_chathealthy.py).
    #
    # not per-env. Data promotion does not touch build/version/framework.
    # Log the current global build for the operator's audit trail.
    current_record = client["frontEndAdmin"]["BuildVersions"].find_one(sort=[("from", -1)])
    current_build = current_record["build"] if current_record else "unknown"
    log.info("Current global build: %s (frontEndAdmin.BuildVersions latest record)", current_build)

    log.info("PROMOTE COMPLETE: %s -> %s", from_env, to_env)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote provider data between environments")
    parser.add_argument("--from", dest="from_env", required=True,
                        help="Source environment (e.g., dev)")
    parser.add_argument("--to", dest="to_env", required=True,
                        help="Target environment (e.g., qa)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making changes")
    args = parser.parse_args()

    promote_data(args.from_env, args.to_env, args.dry_run)
