"""provider_verification_runbook.py - ProviderVerificationRunbook.

Published to Automation Account ChatHealthyJobManager as runbook name
'ProviderVerificationRunbook'. Triggered on demand (webhook or portal).

Confirms the 2026-09-28 provider-record changes against the latest published
provider collection, in-cluster. An Automation runbook is a single file with pip
wheels only -- it carries no `pipeline` package -- so every check here is
self-contained on chathealthy_lib + pymongo, resolving the collection from the
PipelineConfig record rather than importing the pipeline registry.

Steps:
  1. Resume the pipeline cluster (it is paused between runs).
  2. Resolve data_version from pipeline.loaded_metadata (newest loaded) and the
     provider collection (db + Provider_v_N) from
     PipelineConfig.dataset_versions[provider].public_data_name.
  3. Run four checks:
       - active.is_active is one of the four values, always present.
       - every eligible practice address is the -1 pending sentinel OR resolved.
       - the collection holds exactly the target index set (no parallel-array).
       - PublicStaging is empty (successful run cleared it).
  4. Write a result document to pipelineAdmin.verification_runs and exit 0 on
     pass, 1 on any failure.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import datetime
import json
import os
import sys
import time
import urllib.error
import urllib.request
from urllib.request import (HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm,
                            build_opener)

try:
    import automationassets  # type: ignore[import-not-found]
    for _k in ("ATLAS_PIPELINE_PUBLIC_KEY", "ATLAS_PIPELINE_PRIVATE_KEY",
               "ATLAS_PROJECT_ID"):
        try:
            os.environ[_k] = str(automationassets.get_automation_variable(_k))
        except Exception:
            pass
except ImportError:
    pass

from chathealthy_lib.logging_service import set_mongo_log_identity  # noqa: E402
set_mongo_log_identity("pipelineEditor")
os.environ.setdefault("CH_SPACE_NAME", "provider-verification")
os.environ.setdefault("CH_COMPONENT", "provider-verification")
os.environ.setdefault("ENV_PREFIX", os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")

try:
    import dns.resolver  # type: ignore[import-not-found]
    _r = dns.resolver.Resolver(configure=False)
    _r.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    dns.resolver.default_resolver = _r
except ImportError:
    pass

from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

_log = ChatHealthyLoggingService()
_PIPELINE_CLUSTER = "ChatHealthyDataPipelines"
_ATLAS = "https://cloud.mongodb.com/api/atlas/v1.0"
_ACTIVE_VALUES = {"Default_true", "history_true", "original_false", "history_false"}
_PRACTICE_TYPES = {"practice", "secondary_practice"}
_REAL_SOURCES = {"census_batch", "google_maps"}
_TARGET_INDEXES = {
    "_id_", "npi_1", "business_state_taxonomy",
    "practice_addresses.county.source_1", "entity_taxonomy",
    "entity_practice_state", "practice_addresses.coordinates.source_1",
    "active.is_active_1",
}


def _resume_cluster() -> None:
    project = os.environ.get("ATLAS_PROJECT_ID", "").strip()
    public = os.environ.get("ATLAS_PIPELINE_PUBLIC_KEY", "").strip()
    private = os.environ.get("ATLAS_PIPELINE_PRIVATE_KEY", "").strip()
    if not (project and public and private):
        raise ChatHealthyException(
            mode="atlas_credential_absent", component="provider_verification",
            message="ATLAS_PIPELINE_PUBLIC_KEY/PRIVATE_KEY and ATLAS_PROJECT_ID "
                    "are required to resume the cluster.")
    url = f"{_ATLAS}/groups/{project}/clusters/{_PIPELINE_CLUSTER}"
    mgr = HTTPPasswordMgrWithDefaultRealm()
    mgr.add_password(None, url, public, private)
    opener = build_opener(HTTPDigestAuthHandler(mgr))
    try:
        req = urllib.request.Request(
            url, data=json.dumps({"paused": False}).encode("utf-8"),
            method="PATCH", headers={"Content-Type": "application/json"})
        opener.open(req, timeout=90).read()
    except urllib.error.HTTPError as exc:
        _log.LogPipeline("INFO", "resume PATCH returned %s (may already be un-pausing)", exc.code)
    # Wait until a write lands (a ping can answer while write-locked).
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            client = ChatHealthyMongoUtilities().getConnection("pipelineEditor", _PIPELINE_CLUSTER)
            client["pipelineAdmin"]["cluster_readiness"].replace_one(
                {"_id": "verification-readiness"},
                {"_id": "verification-readiness",
                 "written_at": datetime.datetime.utcnow()}, upsert=True)
            _log.LogPipeline("INFO", "pipeline cluster is up and takes writes")
            return
        except Exception as exc:  # noqa: BLE001
            _log.LogPipeline("INFO", "cluster not ready yet (%s); waiting", type(exc).__name__)
            time.sleep(10)
    raise ChatHealthyException(
        mode="cluster_unavailable", component="provider_verification",
        message="pipeline cluster did not accept a write within 600s")


def _resolve_provider_collection():
    fe = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]
    lm = fe["pipeline.loaded_metadata"].find_one(
        {"operationally_fit": True}, sort=[("loaded_at", -1)])
    if not lm:
        raise ChatHealthyException(
            mode="not_found", component="provider_verification",
            message="no operationally_fit provider collection in loaded_metadata")
    data_version = int(lm.get("data_version"))
    # publish_provider is gone, so no provider load-state doc is written; the
    # newest loaded_metadata is another source's, but carries the same fire's
    # data_version. Resolve the provider collection from config so verification
    # never targets a different source's collection.
    cfg = fe["PipelineConfig"].find_one({"env": os.environ.get("ENV_PREFIX", "dev")}) or {}
    public_db = None
    coll_base = None
    for dv in (cfg.get("dataset_versions") or []):
        if dv.get("source_name") == "provider":
            public_db, _, coll_base = (dv.get("public_data_name") or "").partition(".")
            break
    coll_name = f"{coll_base}_v_{data_version}" if coll_base else None
    if not (public_db and coll_name):
        raise ChatHealthyException(
            mode="config_error", component="provider_verification",
            message=f"could not resolve provider collection: db={public_db} coll={coll_name}")
    data = ChatHealthyMongoUtilities().getConnection("pipelineEditor", _PIPELINE_CLUSTER)
    _log.LogPipeline("INFO", "verifying %s.%s (data_version=%d)", public_db, coll_name, data_version)
    return data[public_db][coll_name], data_version


def _run_checks(coll) -> list:
    failures = []
    total = coll.count_documents({})
    if total == 0:
        failures.append("served collection is empty")
        return failures

    bad_active = list(coll.find(
        {"$or": [{"active": {"$exists": False}},
                 {"active.is_active": {"$nin": list(_ACTIVE_VALUES)}}]},
        {"npi": 1}).limit(10))
    if bad_active:
        failures.append(f"active.is_active missing/invalid on {len(bad_active)}+ docs: "
                        f"{[d.get('npi') for d in bad_active]}")

    coord_offenders = []
    for doc in coll.find({}, {"npi": 1, "practice_addresses": 1}):
        for addr in (doc.get("practice_addresses") or []):
            if addr.get("address_type") not in _PRACTICE_TYPES:
                continue
            c = addr.get("coordinates")
            if not isinstance(c, dict):
                coord_offenders.append((doc.get("npi"), "no coordinates"))
                break
            lat, lon, src = c.get("latitude"), c.get("longitude"), c.get("source")
            pending = (lat == -1 and lon == -1 and src == "pending")
            real = (lat not in (None, -1) and lon not in (None, -1) and src in _REAL_SOURCES)
            if not (pending or real):
                coord_offenders.append((doc.get("npi"), c))
                break
        if len(coord_offenders) >= 10:
            break
    if coord_offenders:
        failures.append(f"coordinates neither -1 nor resolved on {len(coord_offenders)}+ addrs: "
                        f"{coord_offenders}")

    names = {ix["name"] for ix in coll.list_indexes()}
    missing = _TARGET_INDEXES - names
    extra = names - _TARGET_INDEXES
    if missing:
        failures.append(f"missing indexes: {missing}")
    if extra:
        failures.append(f"unexpected/stale indexes: {extra}")

    staging = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _PIPELINE_CLUSTER)["PublicStaging"]
    left = staging.list_collection_names()
    if left:
        failures.append(f"PublicStaging not empty: {left}")

    return failures


def main() -> int:
    _log.LogPipeline("INFO", "provider verification: starting")
    _resume_cluster()
    coll, data_version = _resolve_provider_collection()
    total = coll.count_documents({})
    failures = _run_checks(coll)
    passed = not failures
    result = {
        "checked_at": datetime.datetime.utcnow(),
        "collection": coll.full_name,
        "data_version": data_version,
        "record_count": total,
        "passed": passed,
        "failures": failures,
    }
    fe = ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    fe["pipelineAdmin"]["verification_runs"].insert_one(dict(result))
    if passed:
        _log.LogPipeline("INFO", "provider verification PASSED: %d records in %s",
                  total, coll.full_name)
        return 0
    _log.LogPipeline("ERROR", "provider verification FAILED: %s",
               ChatHealthyException(mode="verification_failed",
                                    component="provider_verification",
                                    message="; ".join(failures)))
    return 1


if __name__ == "__main__":
    sys.exit(main())
