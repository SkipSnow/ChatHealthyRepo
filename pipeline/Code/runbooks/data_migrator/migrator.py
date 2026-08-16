"""migrator.py - ChatHealthyDataMigrator migrator runbook.

Runs on the Hybrid Worker VM the provisioner created. Performs the
threaded cross-cluster MongoDB collection copy.

Flow:

  1. Read the migration payload (one JSON-encoded `payload` parameter
     in sys.argv[1]). Extract every arg.
  2. Initialize the job-status doc on the pipeline cluster
     (admin.ChatHealthyDataMigrator_jobs).
  3. Wake the source cluster and create a reservation via the existing
     ClusterLifecycleManager pattern. Poll Atlas until the source
     cluster reaches IDLE.
  4. If the destination collection exists, drop and recreate.
  5. Enumerate partitions from thread_criteria. Wildcarded fields are
     enumerated against the source via distinct(field, JobFilter) and
     each distinct value (null/missing counts as one) becomes a thread.
  6. ThreadPoolExecutor: one thread per partition. Each thread reads
     its slice from source and writes to destination via
     bulk_write(ordered=True). Per-thread progress is written into the
     threads[] array of the job-status doc.
  7. Reconcile: sum of per-thread migrated counts MUST equal
     source.count_documents(filter); mismatch = abend.
  8. If preserve_indices=True, mirror every user-defined source index
     on the destination.
  9. finally:
       - Set has_exception on the job doc and write ended_at.
       - On failure: drop destination collection.
       - Always: release source cluster reservation.
       - Fire-and-forget the deprovisioner runbook (do not wait).
       - Log the deprovisioner AA job_id.

Input payload (one JSON-encoded `payload` parameter, read via sys.argv[1]):
    job_id, vm_name, source_cluster, source_database, source_collection,
    destination_cluster, destination_database, destination_collection,
    filter (Mongo-legal dict), thread_criteria (dict),
    preserve_indices (bool), reservation_duration_minutes (int).

Every cluster is reached through ChatHealthyMongoUtilities, which presents
the identity's certificate. No connection string is read or held.

Environment (Automation Variables):
    ATLAS_PUBLIC_KEY / ATLAS_PRIVATE_KEY / ATLAS_PROJECT_ID - for source wake polling.
    AZ_SUBSCRIPTION_ID                    - for deprovisioner job_start.
    AZ_AUTOMATION_RESOURCE_GROUP          - Automation Account RG.
    AZ_AUTOMATION_ACCOUNT                 - ChatHealthyJobManager.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

import base64
import itertools
import json
import os
import sys
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from pymongo import MongoClient, InsertOne
from requests.auth import HTTPDigestAuth
from pipeline_db import PIPELINE_ADMIN_DB

try:
    import automationassets
    for k in ("ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY", "ATLAS_PROJECT_ID",
              "AZ_SUBSCRIPTION_ID", "AZ_AUTOMATION_RESOURCE_GROUP",
              "AZ_AUTOMATION_ACCOUNT",
              "KEY_VAULT_URI", "AUTOMATION_ENV_PREFIX"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    automationassets = None  # type: ignore

# Wire Mongo logging BEFORE ChatHealthyLoggingService() below.
os.environ.setdefault("CH_SPACE_NAME", "data-migrator-migrator")
os.environ.setdefault("CH_COMPONENT", "data-migrator-migrator")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")

log = ChatHealthyLoggingService()
_STATUS_DB = "admin"
_STATUS_COLLECTION = "ChatHealthyDataMigrator_jobs"
_BATCH_SIZE = 5000
_DEPROVISIONER_RUNBOOK = "ChatHealthyDataMigratorDeprovisioner"
_AUTOMATION_API = "2023-11-01"
_SOURCE_WAKE_POLL_SEC = 15
_SOURCE_WAKE_TIMEOUT_SEC = 60 * 60


def _read_payload() -> dict:
    """Migrator is fired by provisioner via PUT /jobs (runOn=HWG). The
    sender base64-encodes the payload JSON so it survives legacy AA's
    parameter quote-stripping. sys.argv[1] is a base64 string."""
    if len(sys.argv) < 2:
        raise ChatHealthyException(mode="runtime_error", message="no payload: sys.argv[1] missing")
    return json.loads(base64.b64decode(sys.argv[1]).decode("utf-8"))


def _connection_string_for_cluster(cluster_name: str) -> str:
    env_var = f"MONGO_CLUSTER_{cluster_name}_connectionString"
    if env_var in os.environ:
        return os.environ[env_var]
    if automationassets is not None:
        try:
            val = str(automationassets.get_automation_variable(env_var))
            if val:
                os.environ[env_var] = val
                return val
        except Exception:
            pass
    raise ChatHealthyException(mode="runtime_error", message=f"No connection string for cluster {cluster_name!r}: env var "
        f"{env_var!r} is not set.")


class StatusDoc:
    def __init__(self, pipeline_client: MongoClient, job_id: str):
        self._coll = pipeline_client[_STATUS_DB][_STATUS_COLLECTION]
        self._job_id = job_id
        self._lock = threading.Lock()

    def init(self, base_filter: dict, partitions: list[dict]) -> None:
        """Initialize the job doc and one row per thread. partition_filter
        per-thread is the COMPOSED Mongo query the thread will run (the
        contract's per-thread row carries the actual partition filter, not
        an internal structured representation). status is intentionally
        absent at init - the contract enum is {ok, error}, set on thread
        completion only."""
        now = datetime.now(timezone.utc).isoformat()
        threads = [
            {
                "thread_id": str(i),
                "partition_filter": _compose_slice_filter(base_filter, p),
                "source_read_count": 0,
                "dest_write_count": 0,
                "last_update_ts": now,
            }
            for i, p in enumerate(partitions)
        ]
        self._coll.replace_one(
            {"_id": self._job_id},
            {
                "_id": self._job_id,
                "job_id": self._job_id,
                "started_at": now,
                "ended_at": None,
                "has_exception": False,
                "threads": threads,
            },
            upsert=True,
        )

    def thread_progress(self, thread_id: str, source_read: int, dest_write: int) -> None:
        with self._lock:
            self._coll.update_one(
                {"_id": self._job_id, "threads.thread_id": thread_id},
                {"$set": {
                    "threads.$.source_read_count": source_read,
                    "threads.$.dest_write_count": dest_write,
                    "threads.$.last_update_ts": datetime.now(timezone.utc).isoformat(),
                }},
            )

    def thread_status(self, thread_id: str, status: str) -> None:
        with self._lock:
            self._coll.update_one(
                {"_id": self._job_id, "threads.thread_id": thread_id},
                {"$set": {
                    "threads.$.status": status,
                    "threads.$.last_update_ts": datetime.now(timezone.utc).isoformat(),
                }},
            )

    def finalize(self, has_exception: bool) -> None:
        self._coll.update_one(
            {"_id": self._job_id},
            {"$set": {
                "has_exception": has_exception,
                "ended_at": datetime.now(timezone.utc).isoformat(),
            }},
        )


_atlas_cluster_name_cache: dict = {}


def _hostname_from_conn_str(conn_str: str) -> str:
    """Extract host.id.mongodb.net from a mongodb+srv URI without using regex."""
    if "@" not in conn_str:
        raise ChatHealthyException(mode="runtime_error", message=f"connection string missing '@': {conn_str[:40]!r}")
    after_at = conn_str.split("@", 1)[1]
    return after_at.split("/", 1)[0].split("?", 1)[0]


def _atlas_cluster_name_for(friendly_name: str) -> str:
    """Atlas Management API addresses clusters by their real Atlas cluster
    name (e.g. 'ChatHealthyDataPipelines'), not the friendly alias
    (e.g. 'pipeline'). Resolve at runtime by matching the connection
    string's hostname against the standardSrv URI of each cluster in the
    project. Cached per friendly_name."""
    cached = _atlas_cluster_name_cache.get(friendly_name)
    if cached is not None:
        return cached
    conn_str = _connection_string_for_cluster(friendly_name)
    hostname = _hostname_from_conn_str(conn_str)
    auth = HTTPDigestAuth(os.environ["ATLAS_PUBLIC_KEY"], os.environ["ATLAS_PRIVATE_KEY"])
    url = (
        f"https://cloud.mongodb.com/api/atlas/v2/groups/{os.environ['ATLAS_PROJECT_ID']}"
        f"/clusters"
    )
    r = requests.get(
        url, auth=auth,
        headers={"Accept": "application/vnd.atlas.2023-02-01+json"},
        timeout=15,
    )
    r.raise_for_status()
    for c in r.json().get("results", []):
        srv = (c.get("connectionStrings", {}) or {}).get("standardSrv", "") or ""
        if hostname in srv:
            atlas_name = c["name"]
            _atlas_cluster_name_cache[friendly_name] = atlas_name
            return atlas_name
    raise ChatHealthyException(mode="runtime_error", message=f"No Atlas cluster in project matches hostname {hostname!r} "
        f"(connection string for alias {friendly_name!r})"
    )


def _atlas_cluster_state(cluster_name: str) -> str:
    atlas_name = _atlas_cluster_name_for(cluster_name)
    auth = HTTPDigestAuth(os.environ["ATLAS_PUBLIC_KEY"], os.environ["ATLAS_PRIVATE_KEY"])
    url = (
        f"https://cloud.mongodb.com/api/atlas/v2/groups/{os.environ['ATLAS_PROJECT_ID']}"
        f"/clusters/{atlas_name}"
    )
    r = requests.get(
        url, auth=auth,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/vnd.atlas.2023-02-01+json",
        },
        timeout=15,
    )
    r.raise_for_status()
    return r.json().get("stateName", "UNKNOWN")


def _atlas_resume_cluster(cluster_name: str) -> None:
    atlas_name = _atlas_cluster_name_for(cluster_name)
    auth = HTTPDigestAuth(os.environ["ATLAS_PUBLIC_KEY"], os.environ["ATLAS_PRIVATE_KEY"])
    url = (
        f"https://cloud.mongodb.com/api/atlas/v2/groups/{os.environ['ATLAS_PROJECT_ID']}"
        f"/clusters/{atlas_name}"
    )
    requests.patch(
        url, json={"paused": False}, auth=auth,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/vnd.atlas.2023-02-01+json",
        },
        timeout=30,
    )


def _wait_source_idle(cluster_name: str) -> None:
    deadline = time.time() + _SOURCE_WAKE_TIMEOUT_SEC
    while time.time() < deadline:
        state = _atlas_cluster_state(cluster_name)
        if state == "IDLE":
            return
        time.sleep(_SOURCE_WAKE_POLL_SEC)
    raise ChatHealthyException(mode="runtime_error", message=f"Source cluster {cluster_name} did not reach IDLE within "
        f"{_SOURCE_WAKE_TIMEOUT_SEC}s")


def _frontend_admin_coll():
    # Lifecycle records are operational, and must be readable while the
    # source factory is asleep -- which is exactly when this job consults them.
    fe = ChatHealthyMongoUtilities().getConnection("dataTransferAgent", "ChatHealthyFrontEnd")
    return fe[PIPELINE_ADMIN_DB]["cluster_lifecycle"]


def _reserve_source(cluster_name: str, job_id: str, duration_minutes: int) -> None:
    coll = _frontend_admin_coll()
    now = datetime.now(timezone.utc)
    end = now.timestamp() + duration_minutes * 60
    end_iso = datetime.fromtimestamp(end, tz=timezone.utc).isoformat()
    coll.replace_one(
        {"_id": job_id},
        {
            "_id": job_id,
            "job_id": job_id,
            "requester": "ChatHealthyDataMigrator",
            "cluster_name": cluster_name,
            "expected_duration_minutes": duration_minutes,
            "expected_min_minutes": 0,
            "start_time": now.isoformat(),
            "expected_end_time": end_iso,
            "status": "active",
            "reservation_class": "automated",
        },
        upsert=True,
    )


def _release_source(job_id: str) -> None:
    coll = _frontend_admin_coll()
    coll.delete_one({"_id": job_id})


def _split_prefix(key: str) -> tuple[str, str]:
    """For a thread_criteria/JobFilter key like 'addresses.state' return
    ('addresses', 'state'); for 'state' return ('', 'state'); for a Mongo
    operator key like '$or' return ('', '$or'). Operator-facing keys never
    carry $elemMatch."""
    if "." in key and not key.startswith("$"):
        prefix, leaf = key.rsplit(".", 1)
        return prefix, leaf
    return "", key


class _Missing:
    """The partition claiming documents that carry no value at this path.

    Written as None it became {"path": None}, which selects documents whose
    value IS null -- not those where the path is absent -- and, being an
    ordinary value, it overwrote whatever the JobFilter said about the same
    path. Both faults sent a partition outside the migration's scope.
    """

    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def _prefix_is_array(src_coll, prefix: str) -> bool:
    """Is this path actually an array in the data?

    A dot in a key used to be taken as proof, and it was, while every dotted
    partition key named an entry inside addresses[]. business_address is a
    dotted path onto a single object, and $unwind accepts a non-array operand
    -- so the wrong branch produced right answers slowly, scanning the
    collection instead of seeking the index the split exists to enable, and
    silently dropping documents that carry no such field at all.
    """
    return src_coll.find_one({prefix: {"$type": "array"}}, {"_id": 1}) is not None


def _group_by_prefix(spec: dict) -> tuple[dict, dict]:
    """Partition spec entries into (flat, grouped):
       flat: {key: value} for entries with no dot prefix (top-level fields
             and Mongo operators).
       grouped: {prefix: {leaf: value}} for dotted entries grouped by
             their shared prefix (the array path)."""
    flat: dict = {}
    grouped: dict = {}
    for k, v in (spec or {}).items():
        prefix, leaf = _split_prefix(k)
        if prefix:
            grouped.setdefault(prefix, {})[leaf] = v
        else:
            flat[k] = v
    return flat, grouped


def _materialize_query(flat: dict, grouped: dict,
                       array_prefixes=()) -> dict:
    """Render the operator's dotted-path spec as a real Mongo query:
       a prefix with 2+ leafs becomes a single $elemMatch on that prefix
       (per-element scoping the operator never has to spell out);
       a prefix with exactly 1 leaf stays as a dotted-path predicate."""
    out = dict(flat)
    for prefix, leafs in grouped.items():
        if len(leafs) >= 2 and prefix in (array_prefixes or ()):
            out[prefix] = {"$elemMatch": dict(leafs)}
        else:
            for leaf, val in leafs.items():
                out[f"{prefix}.{leaf}"] = val
    return out


def _group_thread_criteria(thread_criteria: dict) -> dict:
    """Partition thread_criteria into {prefix: {'fixed': {leaf: val},
       'wildcards': [leaf, ...]}} where prefix='' for top-level entries."""
    groups: dict = {}
    for k, v in thread_criteria.items():
        prefix, leaf = _split_prefix(k)
        g = groups.setdefault(prefix, {"fixed": {}, "wildcards": []})
        if v == "*":
            g["wildcards"].append(leaf)
        else:
            g["fixed"][leaf] = v
    return groups


def _enumerate_partitions(src_coll, base_filter: dict, thread_criteria: dict) -> list[dict]:
    if not thread_criteria:
        raise ChatHealthyException(mode="runtime_error", message="thread_criteria is required (missing thread_criteria = abend at entry)"
        )

    jf_flat, jf_grouped = _group_by_prefix(base_filter or {})
    tc_groups = _group_thread_criteria(thread_criteria)
    # Probed once per prefix, before any query is built from it. A dot is not
    # proof of an array, and the answer must be the same everywhere it is
    # asked or two functions will disagree about the same field.
    array_prefixes = {
        pre for pre in set(jf_grouped) | {p for p in tc_groups if p}
        if _prefix_is_array(src_coll, pre)
    }
    jf_query = _materialize_query(jf_flat, jf_grouped, array_prefixes)

    # Enumerate distinct values for each wildcard, in declaration order
    # across thread_criteria. Grouped wildcards use aggregate $unwind +
    # element-level $match (per-element scoping); ungrouped wildcards use
    # distinct(field, JobFilter).
    wildcard_enums: list[tuple[str, str, list]] = []
    for prefix, g in tc_groups.items():
        for leaf in g["wildcards"]:
            if prefix in array_prefixes:
                element_match: dict = {}
                for ek, ev in jf_grouped.get(prefix, {}).items():
                    element_match[f"{prefix}.{ek}"] = ev
                for fk, fv in g["fixed"].items():
                    element_match[f"{prefix}.{fk}"] = fv
                pipeline = [
                    {"$match": jf_query},
                    {"$unwind": {"path": f"${prefix}",
                                 "preserveNullAndEmptyArrays": True}},
                ]
                if element_match:
                    pipeline.append({"$match": element_match})
                pipeline += [
                    {"$group": {"_id": f"${prefix}.{leaf}"}},
                    {"$sort": {"_id": 1}},
                ]
                values = [
                    _MISSING if d["_id"] is None else d["_id"]
                    for d in src_coll.aggregate(pipeline, allowDiskUse=True)
                ]
            else:
                field = f"{prefix}.{leaf}" if prefix else leaf
                values = list(src_coll.distinct(field, jf_query))
                # $and, not a merged dict: when the JobFilter constrains this
                # same path, a merged key overwrites it and the probe asks a
                # wider question than the migration is scoped to.
                absent = src_coll.find_one(
                    {"$and": [jf_query, {field: {"$exists": False}}]},
                    {"_id": 1})
                if absent is not None and _MISSING not in values:
                    values.append(_MISSING)
            if not values:
                values = [None]
            wildcard_enums.append((prefix, leaf, values))

    def _base_partition() -> dict:
        p: dict = {"_groups": {}, "_top_fixed": {},
                   "_array_prefixes": sorted(array_prefixes)}
        for prefix, g in tc_groups.items():
            if prefix and (g["fixed"] or g["wildcards"]):
                p["_groups"][prefix] = {"fixed": dict(g["fixed"]), "wildcards": {}}
            elif not prefix:
                for fk, fv in g["fixed"].items():
                    p["_top_fixed"][fk] = fv
        return p

    if not wildcard_enums:
        return [_base_partition()]

    partitions: list[dict] = []
    for combo in itertools.product(*[e[2] for e in wildcard_enums]):
        partition = _base_partition()
        for (prefix, leaf, _), val in zip(wildcard_enums, combo):
            if prefix:
                partition["_groups"][prefix]["wildcards"][leaf] = val
            else:
                partition["_top_fixed"][leaf] = val
        partitions.append(partition)
    return partitions


def _compose_slice_filter(base_filter: dict, partition: dict) -> dict:
    """Render the per-thread Mongo query. Combines JobFilter + this
    partition's thread_criteria assignments. For any prefix that ends up
    with 2+ constraints (whether from JobFilter, thread_criteria fixed
    entries, or wildcard assignments), the composed query uses a single
    $elemMatch on that prefix - per-element scoping is preserved
    regardless of which side supplied which constraint."""
    jf_flat, jf_grouped = _group_by_prefix(base_filter or {})
    combined: dict = dict(jf_flat)

    for k, v in partition.get("_top_fixed", {}).items():
        combined[k] = v

    all_prefixes = set(jf_grouped.keys()) | set(partition.get("_groups", {}).keys())
    for prefix in all_prefixes:
        # Every constraint on a leaf is kept. Assigning them into one dict let
        # the partition's wildcard replace the JobFilter's constraint on the
        # same leaf, so a run scoped to two states could yield a partition
        # scoped to neither.
        em: dict[str, list] = {}
        for ek, ev in jf_grouped.get(prefix, {}).items():
            em.setdefault(ek, []).append(ev)
        tc_group = partition.get("_groups", {}).get(prefix, {})
        for fk, fv in tc_group.get("fixed", {}).items():
            em.setdefault(fk, []).append(fv)
        for wk, wv in tc_group.get("wildcards", {}).items():
            em.setdefault(wk, []).append(wv)

        if len(em) >= 2 and prefix in set(partition.get("_array_prefixes") or ()):
            elem: dict = {}
            for leaf, vals in em.items():
                for val in vals:
                    _constrain(elem, leaf, val)
            _constrain(combined, prefix, {"$elemMatch": elem})
        else:
            for leaf, vals in em.items():
                for val in vals:
                    _constrain(combined, f"{prefix}.{leaf}", val)
    return combined


def _raise_partitions_do_not_cover(uncovered: int) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="config_error",
        component="data_migrator",
        message=(f"{uncovered} source document(s) match the JobFilter and no "
                 f"partition, so no thread would copy them and the "
                 f"reconciliation could not see the loss."))


def _constrain(query: dict, key: str, value) -> None:
    """Add a constraint without discarding one already on that key.

    A partition wildcard assigned straight into the query replaced whatever
    the JobFilter had said about the same path, so a run scoped to two states
    could produce a partition scoped to neither.
    """
    if value is _MISSING:
        value = {"$exists": False}
    if key in query:
        existing = query.pop(key)
        query.setdefault("$and", []).extend([{key: existing}, {key: value}])
        return
    # Once a key has moved into $and it stays there. Putting the third
    # constraint back at the top level is correct -- Mongo ands top-level
    # keys with $and -- but it means the same query carries one key in two
    # shapes, and the next reader has to know that to reason about it.
    if any(key in clause for clause in query.get("$and", [])):
        query.setdefault("$and", []).append({key: value})
        return
    query[key] = value


def _migrate_slice(src_coll, dst_coll, base_filter: dict, partition: dict,
                   thread_id: str, status_doc: StatusDoc) -> tuple[int, int]:
    combined = _compose_slice_filter(base_filter, partition)
    cursor = src_coll.find(combined, no_cursor_timeout=False).batch_size(_BATCH_SIZE)
    batch: list[InsertOne] = []
    read_count = 0
    write_count = 0
    for doc in cursor:
        batch.append(InsertOne(doc))
        read_count += 1
        if len(batch) >= _BATCH_SIZE:
            dst_coll.bulk_write(batch, ordered=True)
            write_count += len(batch)
            batch.clear()
            status_doc.thread_progress(thread_id, read_count, write_count)
    if batch:
        dst_coll.bulk_write(batch, ordered=True)
        write_count += len(batch)
        batch.clear()
        status_doc.thread_progress(thread_id, read_count, write_count)
    status_doc.thread_status(thread_id, "ok")
    return read_count, write_count


def _mirror_indexes(src_coll, dst_coll) -> int:
    """Mirror every user-defined source index to the destination, then
    explicitly verify every name is present in dst_coll.list_indexes()
    before returning. PyMongo's create_index blocks until the server
    acks (which on Atlas 4.2+ means the build is complete), but
    list_indexes() verification provides an explicit guarantee
    independent of that behavior."""
    expected_names = []
    for idx in src_coll.list_indexes():
        name = idx.get("name", "")
        if name == "_id_":
            continue
        key = list(idx["key"].items())
        opts = {
            k: v for k, v in idx.items()
            if k not in ("v", "key", "ns", "background", "name", "textIndexVersion",
                         "2dsphereIndexVersion", "wildcardProjection")
        }
        opts["name"] = name
        dst_coll.create_index(key, **opts)
        # If the index is not present immediately after create_index,
        # the build did not land and we must not proceed to load data.
        dst_names_after = {ix["name"] for ix in dst_coll.list_indexes()}
        if name not in dst_names_after:
            raise ChatHealthyException(mode="runtime_error", message=f"_mirror_indexes: create_index for {name!r} acked but the "
                f"index is not present in dst_coll.list_indexes() afterward; "
                f"refusing to write any docs."
            )
        expected_names.append(name)

    # Final check: every expected name must be present together.
    final_names = {ix["name"] for ix in dst_coll.list_indexes()}
    missing = [n for n in expected_names if n not in final_names]
    if missing:
        raise ChatHealthyException(mode="runtime_error", message=f"_mirror_indexes: final verification failed; missing on destination: {missing!r}")
    return len(expected_names)


def _mi_token() -> str:
    endpoint = os.environ["IDENTITY_ENDPOINT"]
    header = os.environ["IDENTITY_HEADER"]
    r = requests.get(
        endpoint,
        params={"resource": "https://management.azure.com/", "api-version": "2019-08-01"},
        headers={"X-IDENTITY-HEADER": header},
        timeout=15,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def _fire_deprovisioner(job_id: str, vm_name: str, request_guid: str) -> str:
    """Fire-and-forget the deprovisioner runbook. Returns the AA job_id.
    Forwards request_guid so the deprovisioner can log it (contract slide 4:
    every component forwards and logs the request_guid)."""
    sub = os.environ["AZ_SUBSCRIPTION_ID"]
    aa_rg = os.environ["AZ_AUTOMATION_RESOURCE_GROUP"]
    aa = os.environ["AZ_AUTOMATION_ACCOUNT"]
    aa_job_id = str(uuid.uuid4())
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/jobs/{aa_job_id}?api-version={_AUTOMATION_API}"
    )
    encoded = base64.b64encode(json.dumps({
        "job_id": job_id, "vm_name": vm_name, "request_guid": request_guid,
    }).encode("utf-8")).decode("ascii")
    body = {
        "properties": {
            "runbook": {"name": _DEPROVISIONER_RUNBOOK},
            "parameters": {"payload": encoded},
        }
    }
    token = _mi_token()
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise ChatHealthyException(mode="runtime_error", message=f"Start deprovisioner failed: HTTP {r.status_code} {r.text[:500]}")
    return aa_job_id


def _assert_reconciled(success_writes: int, source_total: int) -> None:
    """Reconciliation check. Raises, never logs.

    Extracted so _main can emit its final status record without also being a
    raising function -- Rule-005 statement 3: the thrower does not log, the
    catcher does. _main's except block already sets has_exception and its
    finally block logs the outcome, so behaviour is unchanged.
    """
    if success_writes != source_total:
        raise ChatHealthyException(
            mode="runtime_error",
            message=(
                f"Reconciliation mismatch: migrated={success_writes} "
                f"source={source_total}"
            ),
        )


def _main():
    global _request_guid
    payload = _read_payload()
    _request_guid = payload.get("request_guid", "?")
    if payload.get("health_check") is True:
        return
    job_id = payload["job_id"]
    vm_name = payload["vm_name"]
    src_cluster = payload["source_cluster"]
    src_db_name = payload["source_database"]
    src_coll_name = payload["source_collection"]
    dst_cluster = payload["destination_cluster"]
    dst_db_name = payload["destination_database"]
    dst_coll_name = payload["destination_collection"]
    base_filter = payload.get("filter") or {}
    thread_criteria = payload.get("thread_criteria") or {}
    preserve_indexes = bool(payload.get("preserve_indices", True))
    duration_min = int(payload.get("reservation_duration_minutes") or 60)


    # Ensure the per-cluster env vars are populated (KV/AutomationVariable
    # lookup inside _connection_string_for_cluster), then route each client
    # through the utility which reads the same env var it just populated.
    _connection_string_for_cluster(src_cluster)
    _connection_string_for_cluster(dst_cluster)
    # The crossing: read the factory, write the front end, keep status in
    # admin. Three connections, one identity -- the only principal permitted
    # to write PipelinePublicHealthData.
    src_client = ChatHealthyMongoUtilities().getConnection("dataTransferAgent", "ChatHealthyDataPipelines")
    dst_client = ChatHealthyMongoUtilities().getConnection("dataTransferAgent", "ChatHealthyFrontEnd")
    pipeline_client = ChatHealthyMongoUtilities().getConnection("dataTransferAgent", "ChatHealthyFrontEnd")

    src_coll = src_client[src_db_name][src_coll_name]
    dst_coll = dst_client[dst_db_name][dst_coll_name]

    status_doc = StatusDoc(pipeline_client, job_id)
    has_exception = False
    success_writes = 0
    try:
        # Everything from Atlas wake onwards lives inside this try/finally
        # so the deprovisioner fires on AB end regardless of where we die.
        _atlas_resume_cluster(src_cluster)
        _reserve_source(src_cluster, job_id, duration_min)
        _wait_source_idle(src_cluster)

        if dst_coll_name in dst_client[dst_db_name].list_collection_names():
            dst_client[dst_db_name].drop_collection(dst_coll_name)
        dst_client[dst_db_name].create_collection(dst_coll_name)

        # Mirror indexes BEFORE the threadpool so any unique constraints
        # (e.g. the NPI unique index on providers) enforce duplicate-write
        # detection during the load, not after - and so the destination
        # collection holds its source-mirror invariant even on partial-write
        # paths.
        if preserve_indexes:
            created = _mirror_indexes(src_coll, dst_coll)

        partitions = _enumerate_partitions(src_coll, base_filter, thread_criteria)
        status_doc.init(base_filter, partitions)

        with ThreadPoolExecutor(max_workers=len(partitions)) as pool:
            futures = {
                pool.submit(
                    _migrate_slice, src_coll, dst_coll, base_filter, p,
                    str(i), status_doc,
                ): str(i)
                for i, p in enumerate(partitions)
            }
            for fut in as_completed(futures):
                thread_id = futures[fut]
                try:
                    _, write_n = fut.result()
                    success_writes += write_n
                except Exception as e:
                    status_doc.thread_status(thread_id, "error")
                    has_exception = True
                    raise

        # Reconciliation count must be the EFFECTIVE covered scope, not
        # the raw base_filter. A dotted base_filter (e.g.
        # business_address.state in (...))
        # matches docs where DIFFERENT array elements satisfy different
        # parts. The partitions use $elemMatch (per-element scoping), so
        # they only claim docs that have a SINGLE element matching the
        # per-element constraints. Counting the raw base_filter overcounts
        # vs what any thread could write - that gap is permanently
        # uncoverable. The correct target is the union of partition
        # filters: count docs that match at least one partition.
        partition_filters = [
            _compose_slice_filter(base_filter, p) for p in partitions
        ]
        if len(partition_filters) == 1:
            covered_query = partition_filters[0]
        else:
            covered_query = {"$or": partition_filters}
        source_total = src_coll.count_documents(covered_query)
        # The union of partition filters is derived from the partitions that
        # did the writing, so a document no partition claims is absent from
        # both sides and compares equal to itself. Ask separately whether the
        # partitions cover the JobFilter's own population.
        jf_flat, jf_grouped = _group_by_prefix(base_filter or {})
        jf_scope = _materialize_query(
            jf_flat, jf_grouped,
            set(partitions[0].get("_array_prefixes") or ()) if partitions else set())
        uncovered = src_coll.count_documents(
            {"$and": [jf_scope, {"$nor": partition_filters}]}
        ) if partition_filters and jf_scope else 0
        if uncovered:
            _raise_partitions_do_not_cover(uncovered)
        _assert_reconciled(success_writes, source_total)

    except Exception:
        # Threadpool failures set has_exception themselves and re-raise.
        # Pre-threadpool failures (Atlas wake, reservation, partition
        # enumeration) do not, so mark them here before letting the
        # exception propagate to the finally block.
        has_exception = True
        raise
    finally:
        # Each finally step is isolated so a failure in one (e.g. a
        # transient pipeline-cluster blip during finalize) does NOT skip
        # the load-bearing downstream steps - releasing the reservation
        # and firing the deprovisioner.
        try:
            status_doc.finalize(has_exception)
        except Exception as e:
            pass

        if has_exception:
            # On failure the destination is preserved so the failed state
            # can be inspected in place. Log a rich context block to make
            # the failure diagnosable without having to dig.
            try:
                dst_existing = dst_coll_name in dst_client[dst_db_name].list_collection_names()
                dst_count = (
                    dst_client[dst_db_name][dst_coll_name].count_documents({})
                    if dst_existing else None
                )
                dst_indexes = (
                    [ix["name"] for ix in dst_client[dst_db_name][dst_coll_name].list_indexes()]
                    if dst_existing else None
                )
            except Exception as e:
                dst_existing, dst_count, dst_indexes = None, None, f"<count/list_indexes failed: {e!r}>"

        try:
            _release_source(job_id)
        except Exception as e:
            pass

        deprov_aa_job_id = None
        try:
            deprov_aa_job_id = _fire_deprovisioner(job_id, vm_name, _request_guid)
        except Exception as e:
            pass

        log.info(json.dumps({
            "migrator_status": "error" if has_exception else "ok",
            "job_id": job_id,
            "deprovisioner_aa_job_id": deprov_aa_job_id,
        }))


try:
    _main()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Migrator failed: %s", tb)
    ChatHealthyLoggingService().info(json.dumps({"migrator_status": "error", "error": tb[-1500:]}))
    sys.exit(1)
