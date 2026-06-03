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

Environment (Automation Variables):
    MONGO_CLUSTER_<name>_connectionString - one per managed Atlas cluster.
    MONGO_connectionString                - pipeline cluster (status doc).
    MONGO_FRONTEND_connectionString       - front-end cluster (admin.cluster_lifecycle).
    ATLAS_PUBLIC_KEY / ATLAS_PRIVATE_KEY / ATLAS_PROJECT_ID - for source wake polling.
    AZ_SUBSCRIPTION_ID                    - for deprovisioner job_start.
    AZ_AUTOMATION_RESOURCE_GROUP          - Automation Account RG.
    AZ_AUTOMATION_ACCOUNT                 - ChatHealthyJobManager.
"""
import itertools
import json
import logging
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

try:
    import automationassets
    for k in ("MONGO_connectionString", "MONGO_FRONTEND_connectionString",
              "ATLAS_PUBLIC_KEY", "ATLAS_PRIVATE_KEY", "ATLAS_PROJECT_ID",
              "AZ_SUBSCRIPTION_ID", "AZ_AUTOMATION_RESOURCE_GROUP",
              "AZ_AUTOMATION_ACCOUNT"):
        try:
            os.environ[k] = str(automationassets.get_automation_variable(k))
        except Exception:
            pass
except ImportError:
    automationassets = None  # type: ignore

_request_guid = "?"


class _RGFilter(logging.Filter):
    def filter(self, record):
        record.request_guid = _request_guid
        return True


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [rg=%(request_guid)s] %(message)s",
)
log = logging.getLogger("migrator")
log.addFilter(_RGFilter())

_STATUS_DB = "admin"
_STATUS_COLLECTION = "ChatHealthyDataMigrator_jobs"
_BATCH_SIZE = 5000
_DEPROVISIONER_RUNBOOK = "ChatHealthyDataMigratorDeprovisioner"
_AUTOMATION_API = "2023-11-01"
_SOURCE_WAKE_POLL_SEC = 15
_SOURCE_WAKE_TIMEOUT_SEC = 15 * 60


def _read_payload() -> dict:
    if len(sys.argv) < 2:
        raise RuntimeError("no payload: sys.argv[1] missing")
    return json.loads(sys.argv[1])


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
    raise RuntimeError(
        f"No connection string for cluster {cluster_name!r}: env var "
        f"{env_var!r} is not set."
    )


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


def _atlas_cluster_state(cluster_name: str) -> str:
    auth = HTTPDigestAuth(os.environ["ATLAS_PUBLIC_KEY"], os.environ["ATLAS_PRIVATE_KEY"])
    url = (
        f"https://cloud.mongodb.com/api/atlas/v2/groups/{os.environ['ATLAS_PROJECT_ID']}"
        f"/clusters/{cluster_name}"
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
    auth = HTTPDigestAuth(os.environ["ATLAS_PUBLIC_KEY"], os.environ["ATLAS_PRIVATE_KEY"])
    url = (
        f"https://cloud.mongodb.com/api/atlas/v2/groups/{os.environ['ATLAS_PROJECT_ID']}"
        f"/clusters/{cluster_name}"
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
        log.info("Source cluster %s state=%s", cluster_name, state)
        if state == "IDLE":
            return
        time.sleep(_SOURCE_WAKE_POLL_SEC)
    raise RuntimeError(
        f"Source cluster {cluster_name} did not reach IDLE within "
        f"{_SOURCE_WAKE_TIMEOUT_SEC}s"
    )


def _frontend_admin_coll():
    fe = MongoClient(os.environ["MONGO_FRONTEND_connectionString"])
    return fe["admin"]["cluster_lifecycle"]


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


def _materialize_query(flat: dict, grouped: dict) -> dict:
    """Render the operator's dotted-path spec as a real Mongo query:
       a prefix with 2+ leafs becomes a single $elemMatch on that prefix
       (per-element scoping the operator never has to spell out);
       a prefix with exactly 1 leaf stays as a dotted-path predicate."""
    out = dict(flat)
    for prefix, leafs in grouped.items():
        if len(leafs) >= 2:
            out[prefix] = {"$elemMatch": dict(leafs)}
        else:
            leaf, val = next(iter(leafs.items()))
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
        raise RuntimeError(
            "thread_criteria is required (missing thread_criteria = abend at entry)"
        )

    jf_flat, jf_grouped = _group_by_prefix(base_filter or {})
    jf_query = _materialize_query(jf_flat, jf_grouped)
    tc_groups = _group_thread_criteria(thread_criteria)

    # Enumerate distinct values for each wildcard, in declaration order
    # across thread_criteria. Grouped wildcards use aggregate $unwind +
    # element-level $match (per-element scoping); ungrouped wildcards use
    # distinct(field, JobFilter).
    wildcard_enums: list[tuple[str, str, list]] = []
    for prefix, g in tc_groups.items():
        for leaf in g["wildcards"]:
            if prefix:
                element_match: dict = {}
                for ek, ev in jf_grouped.get(prefix, {}).items():
                    element_match[f"{prefix}.{ek}"] = ev
                for fk, fv in g["fixed"].items():
                    element_match[f"{prefix}.{fk}"] = fv
                pipeline = [
                    {"$match": jf_query},
                    {"$unwind": f"${prefix}"},
                ]
                if element_match:
                    pipeline.append({"$match": element_match})
                pipeline += [
                    {"$group": {"_id": f"${prefix}.{leaf}"}},
                    {"$sort": {"_id": 1}},
                ]
                values = [d["_id"] for d in src_coll.aggregate(pipeline, allowDiskUse=True)]
            else:
                values = src_coll.distinct(leaf, jf_query)
            if not values:
                values = [None]
            wildcard_enums.append((prefix, leaf, values))

    def _base_partition() -> dict:
        p: dict = {"_groups": {}, "_top_fixed": {}}
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
        em: dict = {}
        for ek, ev in jf_grouped.get(prefix, {}).items():
            em[ek] = ev
        tc_group = partition.get("_groups", {}).get(prefix, {})
        for fk, fv in tc_group.get("fixed", {}).items():
            em[fk] = fv
        for wk, wv in tc_group.get("wildcards", {}).items():
            em[wk] = wv
        if len(em) >= 2:
            combined[prefix] = {"$elemMatch": em}
        elif len(em) == 1:
            leaf, val = next(iter(em.items()))
            combined[f"{prefix}.{leaf}"] = val
    return combined


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
    created = 0
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
        created += 1
    return created


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
    body = {
        "properties": {
            "runbook": {"name": _DEPROVISIONER_RUNBOOK},
            "parameters": {"payload": json.dumps({
                "job_id": job_id, "vm_name": vm_name, "request_guid": request_guid,
            })},
        }
    }
    token = _mi_token()
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(
            f"Start deprovisioner failed: HTTP {r.status_code} {r.text[:500]}"
        )
    return aa_job_id


def _main():
    global _request_guid
    payload = _read_payload()
    _request_guid = payload.get("request_guid", "?")
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

    log.info("Migrator begin: job_id=%s src=%s/%s.%s dst=%s/%s.%s",
             job_id, src_cluster, src_db_name, src_coll_name,
             dst_cluster, dst_db_name, dst_coll_name)

    src_uri = _connection_string_for_cluster(src_cluster)
    dst_uri = _connection_string_for_cluster(dst_cluster)
    src_client = MongoClient(src_uri, appname="ChatHealthyDataMigrator")
    dst_client = MongoClient(dst_uri, appname="ChatHealthyDataMigrator")
    pipeline_client = MongoClient(os.environ["MONGO_connectionString"])

    src_coll = src_client[src_db_name][src_coll_name]
    dst_coll = dst_client[dst_db_name][dst_coll_name]

    status_doc = StatusDoc(pipeline_client, job_id)

    log.info("Waking source cluster %s and reserving for %d min", src_cluster, duration_min)
    _atlas_resume_cluster(src_cluster)
    _reserve_source(src_cluster, job_id, duration_min)
    _wait_source_idle(src_cluster)

    if dst_coll_name in dst_client[dst_db_name].list_collection_names():
        log.info("Destination collection %s.%s exists - dropping",
                 dst_db_name, dst_coll_name)
        dst_client[dst_db_name].drop_collection(dst_coll_name)
    dst_client[dst_db_name].create_collection(dst_coll_name)

    partitions = _enumerate_partitions(src_coll, base_filter, thread_criteria)
    log.info("Enumerated %d partitions", len(partitions))
    status_doc.init(base_filter, partitions)

    has_exception = False
    success_writes = 0
    try:
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
                    log.error("Thread %s failed: %r", thread_id, e)
                    raise

        source_total = src_coll.count_documents(base_filter)
        if success_writes != source_total:
            has_exception = True
            raise RuntimeError(
                f"Reconciliation mismatch: migrated={success_writes} source={source_total}"
            )

        if preserve_indexes:
            created = _mirror_indexes(src_coll, dst_coll)
            log.info("Mirrored %d user-defined indexes", created)

    finally:
        # Each finally step is isolated so a failure in one (e.g. transient
        # pipeline-cluster blip during finalize) does NOT skip the load-bearing
        # downstream steps - releasing the reservation and firing the
        # deprovisioner. Per slide 4: automated deprovisioning must work on
        # an ab-end.
        try:
            status_doc.finalize(has_exception)
        except Exception as e:
            log.warning("status_doc.finalize failed (continuing): %r", e)

        if has_exception:
            log.info("Failure path: dropping destination collection %s.%s",
                     dst_db_name, dst_coll_name)
            try:
                dst_client[dst_db_name].drop_collection(dst_coll_name)
            except Exception as e:
                log.warning("Drop destination failed (continuing): %r", e)

        try:
            log.info("Releasing source reservation %s", job_id)
            _release_source(job_id)
        except Exception as e:
            log.warning("Release source reservation failed (continuing): %r", e)

        deprov_aa_job_id = None
        try:
            log.info("Firing deprovisioner for vm=%s (fire-and-forget)", vm_name)
            deprov_aa_job_id = _fire_deprovisioner(job_id, vm_name, _request_guid)
            log.info("Migrator fired deprovisioner: job_id=%s deprovisioner_aa_job_id=%s",
                     job_id, deprov_aa_job_id)
        except Exception as e:
            log.error("Fire deprovisioner FAILED - VM %s will leak: %r", vm_name, e)

        print(json.dumps({
            "migrator_status": "error" if has_exception else "ok",
            "job_id": job_id,
            "deprovisioner_aa_job_id": deprov_aa_job_id,
        }), flush=True)


try:
    _main()
    sys.exit(0)
except Exception:
    tb = traceback.format_exc()
    log.error("Migrator failed: %s", tb)
    print(json.dumps({"migrator_status": "error", "error": tb[-1500:]}), flush=True)
    sys.exit(1)
