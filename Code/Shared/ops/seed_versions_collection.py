"""Seed the frontEndAdmin.BuildVersions collection with the first record.

One-shot. Reads the current latest values from
brain/machine_artifacts/content/version.json and writes a single record to
frontEndAdmin.BuildVersions on the front-end MongoDB cluster.

Schema of the record:
    {
        "_id":       <auto>,
        "builds":    [{"env": "dev"|"qa"|"prod", "build": <int>}, ...],
        "version":   <string>,
        "framework": <string>,
        "from":      <iso utc timestamp when this record became active>
    }

All three env slots are seeded with the same starting build int. Subsequent
bumps advance only the dev slot; qa and prod are touched only via the
promotion writers.

Refuses to run if the collection already has any documents — protects
against double-seeding.

Authorization: BUG-001 step "Seed routine".
Implements the canonical-collection direction approved 2026-05-01.
"""
import json

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
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
from chathealthy_lib.logging_service import ChatHealthyLoggingService

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / "Code" / ".env")


log = ChatHealthyLoggingService()

VERSION_JSON = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "version.json"



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
            _lib = _p / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            from dotenv import load_dotenv as _ld
            _ld(_p / ".env", override=False)
            break
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", "admin")


def extract_seed_values():
    """Read the latest build/version/framework from version.json."""
    if not VERSION_JSON.exists():
        log.error("version.json not found at %s — cannot seed", VERSION_JSON)
        sys.exit(1)
    with open(VERSION_JSON, encoding="utf-8") as f:
        data = json.load(f)

    versions = data.get("versions", {})
    if isinstance(versions, dict):
        versions = versions.get("version", [])
    if not versions:
        log.error("version.json has no version entries — cannot seed")
        sys.exit(1)

    latest_version = versions[-1]
    version_str = latest_version.get("version_number")
    framework_str = latest_version.get("framework_version")

    builds = latest_version.get("builds", {})
    if isinstance(builds, dict):
        builds = builds.get("build", [])
    if not builds:
        log.error("latest version has no build entries — cannot seed")
        sys.exit(1)

    build_num = builds[-1].get("build_number")
    if build_num is None or version_str is None or framework_str is None:
        log.error("missing required field(s) — build=%s version=%s framework=%s",
                  build_num, version_str, framework_str)
        sys.exit(1)

    return int(build_num), str(version_str), str(framework_str)


def main():
    build, version, framework = extract_seed_values()
    log.info("Seed values from version.json: build=%d version=%s framework=%s",
             build, version, framework)

    client = _devops_connection()
    coll = client["frontEndAdmin"]["BuildVersions"]

    existing = coll.count_documents({})
    if existing:
        log.error("frontEndAdmin.BuildVersions already has %d document(s) — refusing to "
                  "seed. Manual intervention required if you intended to "
                  "reseed.", existing)
        sys.exit(2)

    record = {
        "builds": [
            {"env": "dev", "build": build},
            {"env": "qa", "build": build},
            {"env": "prod", "build": build},
        ],
        "version": version,
        "framework": framework,
        "from": datetime.now(timezone.utc).isoformat(),
    }
    result = coll.insert_one(record)
    log.info("Seeded frontEndAdmin.BuildVersions with _id=%s: %s", result.inserted_id, record)


if __name__ == "__main__":
    main()
