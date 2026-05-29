# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

import json
import logging
import os
import time
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local development only (shared file lives one level up at Code/.env)
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

# Logging level — set PIPELINE_DEBUG=true in Azure Function App settings to enable debug output.
# Default is INFO (operational messages). Noisy third-party libraries are always capped at WARNING.
_debug = os.environ.get("PIPELINE_DEBUG", "false").lower() == "true"
logging.getLogger().setLevel(logging.DEBUG if _debug else logging.INFO)
for _noisy in ("azure", "pymongo", "urllib3", "requests"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

import azure.durable_functions as df
import azure.functions as func

from auth import require_auth
from icd10_loader import load_icd10
from count_providers_by_state import count_providers_by_state
from pipeline_health import check_mongo_health
# from idle_monitor import check_and_pause  # disabled — see idle_monitor_timer below
# CountyEnrichment-as-a-standalone-pipeline has been retired (2026-05-28).
# The provider pipeline (ProviderPipeline / provider_pipeline_orchestrator)
# owns the providers collection end-to-end and does enrichment inline inside
# process_assignment_activity. No external county-enrichment orchestrator
# is exposed here. Helper utilities in county_enrichment_job.py
# (_get_crosswalk, _get_maps_county_lookup, _build_states_filter,
# PROVIDERS_COLLECTION) remain importable for the StampEmbeddingVersion
# task and other narrow internal uses.
from instance_warmer import cool_instances_fn, warm_instances_fn
from cluster_lifecycle_manager import ClusterLifecycleManager
from provider_load_manager import (
    attach_practice_locations_fn,
    create_vector_index_fn,
    download_zip_fn,
    drain_staging_fn,
    embed_worker_fn,
    embeddings_orchestrator_fn,
    ensure_preload_indexes_fn,
    ensure_postload_indexes_fn,
    extract_csv_fn,
    multi_practice_addresses_orchestrator_fn,
    partition_file_fn,
    prepare_data_orchestrator_fn,
    provider_load_orchestrator_fn,
    provider_worker_fn,
    reconcile_fn,
    report_fn,
    stamp_embedding_version_fn,
    write_metadata_fn,
)
from load_specialty_data import (
    SPECIALTY_PIPELINE_STEPS,
    run_load_specialty_data,
    specialty_pipeline_orchestrator_fn,
)

# Refactor sub-orchestrations + activities (Skip-authorized parent topology).
from fan_out_workers import fan_out_workers_orchestrator_fn
from pipeline_health_and_zombie_kill import (
    kill_zombie_orchestrations_fn,
    pipeline_health_and_zombie_kill_orchestrator_fn,
    ping_mongo_writable_fn,
)
from normalize_provider_rows import (
    normalize_provider_rows_orchestrator_fn,
    normalize_provider_rows_worker_fn,
)
from provider_flags_enrichment import (
    apply_provider_flags_fn,
    provider_flags_enrichment_orchestrator_fn,
)
from throttle_entities import token_bucket_entity_fn
from throttle_entity import throttle_entity_fn
from work_manager_entity import work_manager_entity_fn
from record_worker_orchestrator import record_worker_orchestrator_fn
from recovery_worker_orchestrator import recovery_worker_orchestrator_fn
from recover_county_activity import (
    build_recovery_assignments_fn,
    process_recovery_assignment_fn,
)
from ensure_provider_indexes_activity import ensure_provider_indexes_fn
from source_gather_orchestrator import source_gather_orchestrator_fn
from process_assignment_activity import process_assignment_activity_fn
from chunk_indexer import build_chunk_index_activity_fn
from gather_activities import (
    gather_nppes_zip_activity_fn,
    gather_pl_pfile_activity_fn,
    gather_zip_crosswalk_activity_fn,
    gather_rucc_activity_fn,
    gather_specialty_catalog_activity_fn,
    gather_icd10_activity_fn,
)


def get_csv_metadata_fn(config: dict) -> dict:
    """Read the CSV blob's file_size and header_end (byte offset after the
    header line). Cheap blob op - fast enough to run as a single activity
    at orchestrator startup."""
    from blob_client import get_blob_service
    csv_path = config["csv_path"]
    container = config.get("blob_container", "provider-data")
    blob_client = get_blob_service().get_container_client(container).get_blob_client(csv_path)
    props = blob_client.get_blob_properties()
    file_size = props.size
    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    if "\n" not in header_text:
        raise RuntimeError("Header line exceeds 32KB - unexpected CSV format.")
    header_line = header_text.split("\n")[0]
    header_end = len(header_line.encode("utf-8")) + 1
    logging.info("csv_metadata: file_size=%d header_end=%d", file_size, header_end)
    return {"file_size": file_size, "header_end": header_end}


def provider_pipeline_orchestrator_fn(context):
    """Parent orchestrator for the provider pipeline.

    The one and only code path that produces the front-end providers
    collection ({env}_PublicHealthData.providers). Runs end-to-end with
    no external orchestrations; enrichment, recovery, and embedding all
    happen inline within this orchestration hierarchy.

    Sequence:
      reserve cluster -> ensure provider indexes -> configure throttles ->
      source_gather sub -> read CSV metadata -> seed work_manager ->
      warm instances -> spawn record_worker pool -> task_all -> recovery
      phase (ingest failed cohort -> seed work_manager.repair_chunks ->
      task_all(recovery_worker sub-orchestrations)) -> cool instances ->
      release cluster (finally).
    """
    import azure.durable_functions as df
    from datetime import timedelta

    config = context.get_input() or {}
    load_id = context.instance_id
    config = {**config, "load_id": load_id}

    throttle_cfg = config.get("throttle") or {}
    pool_size = int(config.get("pool_size", config.get("num_workers", 100)))
    batch_size = int(config.get("batch_size", 1000))
    discrepancy_threshold = int(config.get("discrepancy_threshold", 1000))
    config.setdefault("blob_container", "provider-data")

    # Per F-102-S-003-REQ-B-002 "Manage data source freshness": each source
    # has its own staleness TTL declared as an array of {source_name, number_of_days}.
    # Provider pipeline default: NPPES=30 (operator policy of at-most-monthly),
    # others use the research-recommended TTLs. Payload override wins.
    if not config.get("source_staleness"):
        config["source_staleness"] = [
            {"source_name": "nppes_npi",          "number_of_days":  30},
            {"source_name": "census_zcta_county", "number_of_days": 365},
            {"source_name": "usda_rucc",          "number_of_days": 365},
            {"source_name": "icd10_cm",           "number_of_days":  90},
            {"source_name": "nucc",               "number_of_days":  90},
        ]

    yield context.call_activity("wake_cluster_activity", {
        "cluster_name": config["pipeline_cluster"],
        "job_id": load_id,
        "requester": "streaming_pipeline",
        "expected_duration_minutes": config.get("expected_duration_minutes", 120),
    })
    yield context.call_activity("check_cluster_state_activity", {
        "cluster_name": config["pipeline_cluster"],
    })
    yield context.call_activity("register_reservation_activity", {
        "cluster_name": config["pipeline_cluster"],
        "job_id": load_id,
        "requester": "streaming_pipeline",
        "expected_duration_minutes": config.get("expected_duration_minutes", 120),
    })

    # Software-managed provider indexes. Idempotent — already-existing
    # indexes are a no-op. Required by build_chunk_index_activity (npi_1)
    # and build_recovery_assignments_activity (addresses.county.source_1).
    # No operator side-action; the pipeline owns its own index posture.
    #
    # cluster_wait_minutes is a public API parameter: the operator
    # controls how patient the pipeline is with Atlas wake/REPAIRING.
    # The activity polls the cluster every 5s and abends on timeout.
    yield context.call_activity("ensure_provider_indexes_activity", {
        "provider_collection": config.get("provider_collection"),
        "cluster_wait_minutes": int(config.get("cluster_wait_minutes", 20)),
    })

    # Durable throttle inventory shrunk to two semantic gates:
    #   pool_size     — chunk-admission control (concurrency)
    #   source_gather — one-shot startup gate for the gather fan-out
    # Per-API rate limits (census/maps/nppes/openai) are enforced inside the
    # activity via process_assignment_activity._TokenBucket — at the point
    # where the API call actually happens, which the v9 design requires and
    # Microsoft documents (activities cannot call_entity).
    throttle_defaults = {
        "pool_size":     {"refill_rate": float(pool_size), "capacity": pool_size},
        "source_gather": {"refill_rate": 2.0, "capacity": 6},
    }
    throttle_params = {**throttle_defaults, **throttle_cfg}
    throttle_names = list(throttle_params.keys())

    summary = {}
    pipeline_error = None
    try:
        # Clean-environment assumption: reset any stale entity state from a
        # prior run before configuring. throttle.reset clears refill_rate to
        # None so the configure that follows is the sole source of truth.
        context.signal_entity(df.EntityId("work_manager", load_id), "reset", {})
        for name in throttle_names:
            context.signal_entity(df.EntityId("throttle", name), "reset", {})
        for name, params in throttle_params.items():
            context.signal_entity(df.EntityId("throttle", name), "configure", params)

        yield context.call_sub_orchestrator("source_gather_orchestrator", config)

        prepare = yield context.call_sub_orchestrator(
            "prepare_data_orchestrator",
            {
                "load_id": load_id,
                "num_workers": pool_size,
                "blob_container": config["blob_container"],
                "states": config["states"],
                "incremental": False,
                "provider_collection": config.get("provider_collection"),
                "metadata_collection": config.get("metadata_collection"),
            },
        )
        csv_path = prepare["csv_path"]

        # Drain destination collection (business-address state scoped) BEFORE
        # workers start writing. After drain we know the target slice is empty,
        # so the per-record write becomes a plain insert (no upsert handshake).
        yield context.call_activity("drain_staging_activity", {
            "states": config["states"],
            "incremental": False,
            "provider_collection": config.get("provider_collection"),
        })

        meta = yield context.call_activity("get_csv_metadata_activity", {
            "csv_path": csv_path,
            "blob_container": config["blob_container"],
        })

        # Precise record-boundary chunk index. One sequential scan of the CSV
        # emits exact byte ranges per chunk (~batch_size records each).
        # Workers stream their range without any boundary scan. The chunk
        # index is the only chunking path the pipeline supports — there is
        # no coarse byte-range fallback.
        idx = yield context.call_activity("build_chunk_index_activity", {
            "blob_container": config["blob_container"],
            "csv_path": csv_path,
            "file_size": meta["file_size"],
            "header_end": meta["header_end"],
            "batch_size": batch_size,
        })

        yield context.call_entity(
            df.EntityId("work_manager", load_id),
            "seed",
            {
                "file_size": meta["file_size"],
                "header_end": meta["header_end"],
                "batch_size": batch_size,
                "discrepancy_threshold": discrepancy_threshold,
                "chunk_index": idx["chunks"],
            },
        )

        yield context.call_activity("warm_instances_activity", {"num_workers": pool_size})

        worker_cfg_base = {
            "load_id": load_id,
            "csv_path": csv_path,
            "states": config["states"],
            "blob_container": config["blob_container"],
            "provider_collection": config.get("provider_collection"),
            "env_prefix": config.get("env_prefix", "dev"),
            "age_limit_seconds": int(config.get("age_limit_seconds", 6600)),
            "batch_size": batch_size,
        }
        pool = [
            context.call_sub_orchestrator(
                "record_worker_orchestrator",
                {**worker_cfg_base, "worker_id": wid},
            )
            for wid in range(1, pool_size + 1)
        ]
        worker_results = yield context.task_all(pool)

        # ── County recovery phase ─────────────────────────────────────────
        # Per Skip 2026-05-28: ~9% of practice addresses end the load with
        # county.source=geocoder_failed because the in-loop Census batch
        # call missed them. Recover them here inside the same orchestration
        # and the same reservation window using the same ingest -> process
        # -> write-back pattern the CMS load uses:
        #
        #   1. build_recovery_assignments_activity returns chunk_index of
        #      NPI lists — one entry per assignment.
        #   2. work_manager.seed with work_kind="repair_chunks" loads the
        #      second queue.
        #   3. fan out recovery_worker sub-orchestrations; each claims one
        #      assignment, runs the pass chain against the failed cohort
        #      of real provider records, writes back per-NPI.
        recovery_batch_size = int(config.get("recovery_batch_size", 50))
        recovery_pool_size = int(config.get("recovery_pool_size", 20))
        recovery_seed = yield context.call_activity(
            "build_recovery_assignments_activity",
            {
                "load_id": load_id,
                "provider_collection": config.get("provider_collection"),
                "recovery_batch_size": recovery_batch_size,
            },
        )
        recovery_chunks = (recovery_seed or {}).get("chunk_index") or []
        recovery_results = []
        if recovery_chunks:
            yield context.call_entity(
                df.EntityId("work_manager", load_id),
                "seed",
                {
                    "work_kind": "repair_chunks",
                    "chunk_index": recovery_chunks,
                    "batch_size": recovery_batch_size,
                    "discrepancy_threshold": discrepancy_threshold,
                },
            )
            recovery_worker_cfg = {
                "load_id": load_id,
                "provider_collection": config.get("provider_collection"),
                "env_prefix": config.get("env_prefix", "dev"),
                "age_limit_seconds": int(config.get("age_limit_seconds", 6600)),
            }
            recovery_pool = [
                context.call_sub_orchestrator(
                    "recovery_worker_orchestrator",
                    {**recovery_worker_cfg, "worker_id": f"recovery_{wid}"},
                )
                for wid in range(1, recovery_pool_size + 1)
            ]
            recovery_results = yield context.task_all(recovery_pool)

        yield context.call_activity("cool_instances_activity", {})

        summary = {
            "load_id": load_id,
            "pool_size": pool_size,
            "workers_completed": len(worker_results),
            "csv_file_size": meta["file_size"],
            "recovery_chunks": len(recovery_chunks),
            "recovery_total_npis": (recovery_seed or {}).get("total_npis", 0),
            "recovery_workers_completed": len(recovery_results),
        }

        context.signal_entity(
            df.EntityId("work_manager", load_id),
            "emit_metrics",
            {"load_id": load_id},
        )

    except Exception as exc:
        pipeline_error = {"type": type(exc).__name__, "message": str(exc)[:500]}
        raise
    finally:
        yield context.call_activity("release_reservation_activity", {"job_id": load_id})
        # Durable state is transient. After the job ends (Completed, Failed,
        # or Terminated) nothing this run created — sub-orchs, work_manager,
        # throttles, staging blobs — survives. The activity is best-effort
        # and never raises; its counters land in the run metric.
        yield context.call_activity("end_of_job_cleanup_activity", {
            "load_id": load_id,
            "throttle_names": throttle_names,
            "staging_container": config.get("staging_container", "pipeline-staging"),
        })

    return {"load_id": load_id, "summary": summary, "error": pipeline_error}

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

PIPELINE_ROUTE = "Router"

# Synchronous tasks — called directly, return 200
SYNC_TASK_HANDLERS = {
    "LoadSpecialtyData": run_load_specialty_data,
    "LoadICD10": load_icd10,
    "CheckMongoHealth": check_mongo_health,
    "StampEmbeddingVersion": stamp_embedding_version_fn,
    "CountProvidersByState": count_providers_by_state,
}

# Ops Manager tasks — infrastructure only, no pipeline business logic
def _get_mongo_conn():
    return os.environ.get("MONGO_connectionString", "")

def _get_frontend_conn():
    return os.environ.get("MONGO_FRONTEND_connectionString", "")

def _get_ops_manager():
    from cluster_lifecycle_manager import ClusterLifecycleManager
    from pymongo import MongoClient
    frontend_conn = _get_frontend_conn()
    push_fn = None
    email_fn = None
    try:
        from pipeline_health import send_pushover
        push_fn = lambda title, msg: send_pushover(title, msg)
    except Exception:
        pass
    try:
        from pipeline_health import send_admin_notification
        email_fn = lambda subject, text: send_admin_notification(subject, text)
    except Exception:
        pass
    return ClusterLifecycleManager(
        get_db_fn=lambda: MongoClient(frontend_conn),
        push_fn=push_fn,
    )

def _get_ops_agent():
    """Get the OpsManagerAgent — full agent with tools, triage, audit."""
    from cluster_lifecycle_manager import ClusterLifecycleManager
    from ops_manager import OpsManagerAgent
    from pymongo import MongoClient
    conn = _get_mongo_conn()
    frontend_conn = _get_frontend_conn()
    push_fn = None
    email_fn = None
    try:
        from pipeline_health import send_pushover
        push_fn = lambda title, msg: send_pushover(title, msg)
    except Exception:
        pass
    try:
        from pipeline_health import send_admin_notification
        email_fn = lambda subject, text: send_admin_notification(subject, text)
    except Exception:
        pass
    mgr = ClusterLifecycleManager(
        get_db_fn=lambda: MongoClient(frontend_conn),
        push_fn=push_fn,
    )
    return OpsManagerAgent(
        lifecycle_manager=mgr,
        get_db_fn=lambda: MongoClient(conn),
        env_prefix=env_prefix,
        push_fn=push_fn,
        email_fn=email_fn,
    )

def _handle_wake_cluster(payload):
    mgr = _get_ops_manager()
    return mgr.reserve(
        cluster_name=payload.get("cluster_name", "ChatHealthyDataPipelines"),
        job_id=payload.get("job_id", f"manual_{int(__import__('time').time())}"),
        requester=payload.get("requester", "manual"),
        expected_duration_minutes=payload.get("expected_duration_minutes", 60),
    )

def _handle_cluster_status(payload):
    mgr = _get_ops_manager()
    return mgr.status(payload.get("cluster_name", "ChatHealthyDataPipelines"))

def _handle_release(payload):
    mgr = _get_ops_manager()
    return mgr.release(payload.get("job_id", ""))

def _handle_force_release(payload):
    mgr = _get_ops_manager()
    return mgr.force_release_all(payload.get("cluster_name", "ChatHealthyDataPipelines"))

OPS_TASK_HANDLERS = {
    "WakeCluster": _handle_wake_cluster,
    "ClusterStatus": _handle_cluster_status,
    "Release": _handle_release,
    "ForceRelease": _handle_force_release,
}

# Asynchronous tasks — start a Durable orchestrator, return 202 + status URL.
# Retired 2026-05-28: CountyEnrichment (standalone enrichment pipeline — no
# business value on its own); the legacy multi-orchestrator ProviderPipeline
# (delegated enrichment to external CountyEnrichment sub-orchestrators,
# violating the principle that the provider pipeline owns its collection
# end-to-end with no external orchestrations); and the legacy task name
# "StreamingPipeline" — the records-as-messages parent IS the provider
# pipeline, and the business name is "ProviderPipeline". One task entry,
# one orchestrator, one code path to {env}_PublicHealthData.providers.
ASYNC_TASK_ORCHESTRATORS = {
    "ProviderPipeline": "provider_pipeline_orchestrator",
    "SpecialtyPipeline": "specialty_pipeline_orchestrator",
    "SnapshotCollection": "snapshot_collection_orchestrator",
    "PrescriberEvaluateCarePipeline": "prescriber_pipeline_orchestrator",
}

# ── Prescriber pipeline step labels (one canonical string per stage) ─────────
PRESCRIBER_LABEL_VALIDATE  = "Step 1: Validate preconditions"
PRESCRIBER_LABEL_RESERVE   = "Step 2: Reserving cluster"
PRESCRIBER_LABEL_FETCH     = "Step 3: Fetch CMS Part D + OIG LEIE + SAM.gov"
PRESCRIBER_LABEL_LOAD      = "Step 4: Load provider_quality"
PRESCRIBER_LABEL_CROSSWALK = "Step 5: Crosswalk — molecule→indication→ICD-10"
PRESCRIBER_LABEL_ENRICH    = "Step 6: Exclusion flags — OIG/SAM"
PRESCRIBER_LABEL_SPECIALTY = "Step 7: Specialty baselines"
PRESCRIBER_LABEL_EMBED     = "Step 8: Embed prescriber drug/molecule"
PRESCRIBER_LABEL_RELEASE   = "Step 9: Releasing cluster"

PRESCRIBER_PIPELINE_STEPS = [
    PRESCRIBER_LABEL_VALIDATE,
    PRESCRIBER_LABEL_RESERVE,
    PRESCRIBER_LABEL_FETCH,
    PRESCRIBER_LABEL_LOAD,
    PRESCRIBER_LABEL_CROSSWALK,
    PRESCRIBER_LABEL_ENRICH,
    PRESCRIBER_LABEL_SPECIALTY,
    PRESCRIBER_LABEL_EMBED,
    PRESCRIBER_LABEL_RELEASE,
]


# Pipeline step registry — valid step labels per pipeline, with preconditions.
# Used by Router to validate steps[] (or start_step) before dispatching.
# All step values are canonical label strings — same string used as the
# set_custom_status display and the API value (one variable, not two).
PIPELINE_STEP_REGISTRY = {
    "PrescriberEvaluateCarePipeline": {
        "valid_steps": PRESCRIBER_PIPELINE_STEPS,
        "preconditions": {
            PRESCRIBER_LABEL_LOAD:      {"requires_collection": "providers",        "min_docs": 1, "note": "Load builds from providers — need provider data loaded first"},
            PRESCRIBER_LABEL_CROSSWALK: {"requires_collection": "provider_quality", "min_docs": 1, "note": "Crosswalk requires provider_quality records"},
            PRESCRIBER_LABEL_ENRICH:    {"requires_collection": "provider_quality", "min_docs": 1, "note": "Enrich requires provider_quality records"},
        },
    },
    "SpecialtyPipeline": {
        "valid_steps": SPECIALTY_PIPELINE_STEPS,
        "preconditions": {},
    },
}


def _validate_pipeline_steps(task: str, payload: dict) -> tuple:
    """Validate `steps[]` (prescriber) or `start_step` (provider/specialty)
    against the pipeline registry. Returns (valid, error_response)."""
    registry = PIPELINE_STEP_REGISTRY.get(task)
    if not registry:
        return True, None

    valid_steps = registry["valid_steps"]
    requested: list = []

    # Prescriber accepts a list of step labels via `steps`; the Provider and
    # Specialty pipelines accept a single starting label via `start_step`.
    steps = payload.get("steps")
    if steps is not None:
        if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
            return False, json_response({
                "error": "InvalidStepError",
                "message": "steps must be an array of canonical step label strings",
                "task": task,
                "valid_steps": valid_steps,
            }, 400)
        requested = list(steps)

    start_step = payload.get("start_step")
    if start_step is not None:
        if not isinstance(start_step, str):
            return False, json_response({
                "error": "InvalidStepError",
                "message": "start_step must be a canonical step label string",
                "task": task,
                "valid_steps": valid_steps,
            }, 400)
        requested.append(start_step)

    for s in requested:
        if s not in valid_steps:
            return False, json_response({
                "error": "InvalidStepError",
                "message": f"Step {s!r} does not exist in {task}.",
                "valid_steps": valid_steps,
                "task": task,
            }, 400)

    env_prefix = payload.get("env_prefix", "dev")
    for s in requested:
        precond = registry.get("preconditions", {}).get(s)
        if precond and precond.get("min_docs", 0) > 0:
            try:
                from pipeline_db import get_db
                coll_name = precond["requires_collection"]
                count = get_db(env_prefix)[coll_name].count_documents({}, limit=1)
                if count < precond["min_docs"]:
                    return False, json_response({
                        "error": "PreconditionError",
                        "message": f"Step {s!r} requires {coll_name} to have records. {precond.get('note', '')}",
                        "task": task,
                    }, 400)
            except Exception as e:
                logging.warning("Precondition check failed for %r: %s", s, e)

    return True, None


def json_response(payload: dict, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


@app.function_name(name="DevPipelineManagementService")
@app.route(route=PIPELINE_ROUTE)
@app.durable_client_input(client_name="client")
async def dev_pipeline_management(
    req: func.HttpRequest, client: df.DurableOrchestrationClient
) -> func.HttpResponse:
    """Pipeline Router — authenticates, routes sync and async tasks."""
    try:
        user_id, err = require_auth(req)
        if err:
            status_code, message = err
            logging.warning("Authentication failed for route '%s': %s", PIPELINE_ROUTE, message)
            return func.HttpResponse(body=message, status_code=status_code, mimetype="text/plain")

        if req.method == "GET":
            return func.HttpResponse(
                body="This API only supports the HTTP POST method",
                status_code=405,
                mimetype="text/plain",
            )

        try:
            req_body = req.get_json()
        except ValueError:
            return json_response(
                {"success": False, "error": "Request body must contain valid JSON.", "task": None},
                400,
            )

        task = req_body.get("ChatHealthyTask")
        if not isinstance(task, str) or not task.strip():
            return json_response(
                {"success": False, "error": "ChatHealthyTask is required and must be a non-empty string.", "task": task},
                400,
            )

        task = task.strip()
        payload = req_body.get("payload") or {}

        # Per-request debug override — set "debug": true in the payload to enable DEBUG logging.
        if payload.get("debug"):
            logging.getLogger().setLevel(logging.DEBUG)
            for _lib in ("azure", "pymongo", "urllib3", "requests"):
                logging.getLogger(_lib).setLevel(logging.WARNING)

        logging.info("User '%s' requested task '%s'", user_id, task)

        # Ops Manager path — infrastructure only
        if task in OPS_TASK_HANDLERS:
            result = OPS_TASK_HANDLERS[task](payload)
            logging.info("Ops task '%s' completed", task)
            return json_response({"success": True, "task": task, "data": result}, 200)

        # Synchronous pipeline path
        if task in SYNC_TASK_HANDLERS:
            result = SYNC_TASK_HANDLERS[task](payload)
            logging.info("Task '%s' completed for user '%s'", task, user_id)
            return json_response({"success": True, "task": task, "data": result}, 200)

        # Asynchronous path — start Durable orchestrator, return 202
        if task in ASYNC_TASK_ORCHESTRATORS:
            # Validate steps if provided
            valid, err_response = _validate_pipeline_steps(task, payload)
            if not valid:
                return err_response
            orchestrator_name = ASYNC_TASK_ORCHESTRATORS[task]
            instance_id = await client.start_new(orchestrator_name, None, payload)
            logging.info(
                "Started orchestrator '%s' instance '%s' for user '%s'",
                orchestrator_name, instance_id, user_id,
            )
            return client.create_check_status_response(req, instance_id)

        return json_response(
            {"success": False, "error": f"Unknown task: {task}", "task": task},
            400,
        )

    except Exception:
        logging.exception("Unhandled error in DevPipelineManagementService")
        return func.HttpResponse(body="Internal server error", status_code=500, mimetype="text/plain")


# ── Idle Monitor (Timer Trigger) ──────────────────────────────────────────────

# Idle monitor disabled — paused cluster mid-run (fix: pipeline lock needed before re-enabling)
# @app.timer_trigger(schedule="0 */30 * * * *", arg_name="myTimer", run_on_startup=False)
# def idle_monitor_timer(myTimer: func.TimerRequest) -> None:
#     """Auto-pause ChatHealthyDataPipelines if idle longer than IDLE_MONITOR_THRESHOLD_HOURS."""
#     check_and_pause()


# ── Durable Entities ──────────────────────────────────────────────────────────

@app.entity_trigger(context_name="context")
def token_bucket(context: df.DurableEntityContext) -> None:
    token_bucket_entity_fn(context)


@app.entity_trigger(context_name="context")
def throttle(context: df.DurableEntityContext) -> None:
    throttle_entity_fn(context)


@app.entity_trigger(context_name="context")
def work_manager(context: df.DurableEntityContext) -> None:
    work_manager_entity_fn(context)


# ── Durable Orchestrators ─────────────────────────────────────────────────────

@app.orchestration_trigger(context_name="context")
def provider_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    return provider_pipeline_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def record_worker_orchestrator(context: df.DurableOrchestrationContext):
    return record_worker_orchestrator_fn(context)


@app.activity_trigger(input_name="config")
def build_recovery_assignments_activity(config: dict) -> dict:
    return build_recovery_assignments_fn(config)


@app.activity_trigger(input_name="config")
def process_recovery_assignment_activity(config: dict) -> dict:
    return process_recovery_assignment_fn(config)


@app.activity_trigger(input_name="config")
def ensure_provider_indexes_activity(config: dict) -> dict:
    return ensure_provider_indexes_fn(config)


@app.orchestration_trigger(context_name="context")
def recovery_worker_orchestrator(context: df.DurableOrchestrationContext):
    return recovery_worker_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def source_gather_orchestrator(context: df.DurableOrchestrationContext):
    return source_gather_orchestrator_fn(context)


@app.activity_trigger(input_name="config")
def process_assignment_activity(config: dict) -> dict:
    return process_assignment_activity_fn(config)


@app.activity_trigger(input_name="config")
def build_chunk_index_activity(config: dict) -> dict:
    return build_chunk_index_activity_fn(config)


@app.activity_trigger(input_name="config")
def gather_nppes_zip_activity(config: dict) -> dict:
    return gather_nppes_zip_activity_fn(config)


@app.activity_trigger(input_name="config")
def gather_pl_pfile_activity(config: dict) -> dict:
    return gather_pl_pfile_activity_fn(config)


@app.activity_trigger(input_name="config")
def gather_zip_crosswalk_activity(config: dict) -> dict:
    return gather_zip_crosswalk_activity_fn(config)


@app.activity_trigger(input_name="config")
def gather_rucc_activity(config: dict) -> dict:
    return gather_rucc_activity_fn(config)


@app.activity_trigger(input_name="config")
def gather_specialty_catalog_activity(config: dict) -> dict:
    return gather_specialty_catalog_activity_fn(config)


@app.activity_trigger(input_name="config")
def gather_icd10_activity(config: dict) -> dict:
    return gather_icd10_activity_fn(config)


@app.activity_trigger(input_name="config")
def get_csv_metadata_activity(config: dict) -> dict:
    return get_csv_metadata_fn(config)


@app.orchestration_trigger(context_name="context")
def specialty_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    return specialty_pipeline_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def provider_load_orchestrator(context: df.DurableOrchestrationContext):
    return provider_load_orchestrator_fn(context)


# ── Durable Activities ────────────────────────────────────────────────────────

@app.activity_trigger(input_name="config")
def check_mongo_health_activity(config: dict) -> dict:
    return check_mongo_health(config)


@app.activity_trigger(input_name="config")
def check_cluster_state_activity(config: dict) -> dict:
    """Return cluster state for orchestrator polling."""
    mgr = _get_ops_manager()
    return mgr.status(
        config.get("cluster_name", "ChatHealthyDataPipelines"),
        job_id=config.get("job_id"),
    )


@app.activity_trigger(input_name="config")
def download_zip_activity(config: dict) -> str:
    return download_zip_fn(config)


@app.activity_trigger(input_name="config")
def extract_csv_activity(config: dict) -> str:
    return extract_csv_fn(config)


@app.activity_trigger(input_name="config")
def attach_practice_locations_activity(config: dict) -> dict:
    return attach_practice_locations_fn(config)


_US_STATES_DC_TERRITORIES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA",
    "HI", "ID", "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD",
    "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH", "NJ",
    "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC",
    "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
    "DC", "PR", "VI", "GU", "AS", "MP",
]


# stamp_urban_flag_activity (and the urban_flag_enrichment_orchestrator
# that drove it) were a standalone stamping pass under the retired OLD
# ProviderPipeline. In the current ProviderPipeline (streaming) the
# urban flag is stamped inline per-record during process_assignment_activity
# using the rucc.json blob that gather_rucc_activity produces. Removed
# 2026-05-28 along with UrbanFlagStamper.write_data_to_collection.


@app.activity_trigger(input_name="config")
def partition_file_activity(config: dict) -> list:
    return partition_file_fn(config)



@app.activity_trigger(input_name="config")
def drain_staging_activity(config: dict) -> dict:
    return drain_staging_fn(config)


@app.activity_trigger(input_name="config")
def ensure_preload_indexes_activity(config: dict) -> None:
    return ensure_preload_indexes_fn(config)


@app.activity_trigger(input_name="config")
def ensure_postload_indexes_activity(config: dict) -> None:
    return ensure_postload_indexes_fn(config)


@app.activity_trigger(input_name="config")
def load_specialty_data_activity(config: dict) -> dict:
    """Load SpecialtyMetaData from NUCC CSV → blob → pipeline MongoDB."""
    return run_load_specialty_data(config)


@app.activity_trigger(input_name="config")
def write_metadata_activity(config: dict) -> list:
    return write_metadata_fn(config)


@app.activity_trigger(input_name="config")
def wake_cluster_activity(config: dict) -> dict:
    from pipeline_db import get_frontend_mongo
    ClusterLifecycleManager(
        get_db_fn=get_frontend_mongo,
    ).wake(config["cluster_name"], job_id=config.get("job_id"))
    return {"cluster_name": config["cluster_name"], "wake_sent": True}


@app.activity_trigger(input_name="config")
def register_reservation_activity(config: dict) -> dict:
    from pipeline_db import get_frontend_mongo
    return ClusterLifecycleManager(
        get_db_fn=get_frontend_mongo,
    ).reserve(
        cluster_name=config["cluster_name"],
        job_id=config["job_id"],
        requester=config["requester"],
        expected_duration_minutes=config["expected_duration_minutes"],
    )


@app.activity_trigger(input_name="config")
def release_reservation_activity(config: dict) -> dict:
    from pipeline_db import get_frontend_mongo
    return ClusterLifecycleManager(
        get_db_fn=get_frontend_mongo,
    ).release(config["job_id"])


@app.activity_trigger(input_name="config")
def end_of_job_cleanup_activity(config: dict) -> dict:
    from end_of_job_cleanup import end_of_job_cleanup_fn
    return end_of_job_cleanup_fn(config)


@app.activity_trigger(input_name="config")
def warm_instances_activity(config: dict) -> dict:
    return warm_instances_fn(config)


@app.activity_trigger(input_name="config")
def cool_instances_activity(config: dict) -> dict:
    return cool_instances_fn(config)


@app.activity_trigger(input_name="config")
def provider_worker_activity(config: dict) -> dict:
    return provider_worker_fn(config)


@app.activity_trigger(input_name="config")
def reconcile_activity(config: dict) -> dict:
    return reconcile_fn(config)


@app.activity_trigger(input_name="config")
def report_activity(config: dict) -> dict:
    return report_fn(config)


@app.activity_trigger(input_name="config")
def embed_worker_activity(config: dict) -> dict:
    return embed_worker_fn(config)


@app.activity_trigger(input_name="config")
def create_vector_index_activity(config: dict) -> dict:
    return create_vector_index_fn(config)


# ── Refactor sub-orchestrations (Skip-authorized parent topology) ─────────────

@app.orchestration_trigger(context_name="context")
def fan_out_workers_orchestrator(context: df.DurableOrchestrationContext):
    return fan_out_workers_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def pipeline_health_and_zombie_kill_orchestrator(context: df.DurableOrchestrationContext):
    return pipeline_health_and_zombie_kill_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def prepare_data_orchestrator(context: df.DurableOrchestrationContext):
    return prepare_data_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def normalize_provider_rows_orchestrator(context: df.DurableOrchestrationContext):
    return normalize_provider_rows_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def multi_practice_addresses_orchestrator(context: df.DurableOrchestrationContext):
    return multi_practice_addresses_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def provider_flags_enrichment_orchestrator(context: df.DurableOrchestrationContext):
    return provider_flags_enrichment_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def embeddings_orchestrator(context: df.DurableOrchestrationContext):
    return embeddings_orchestrator_fn(context)


@app.activity_trigger(input_name="config")
def ping_mongo_writable_activity(config: dict) -> dict:
    return ping_mongo_writable_fn(config)


@app.activity_trigger(input_name="config")
def kill_zombie_orchestrations_activity(config: dict) -> dict:
    return kill_zombie_orchestrations_fn(config)


@app.activity_trigger(input_name="config")
def normalize_provider_rows_worker_activity(config: dict) -> dict:
    return normalize_provider_rows_worker_fn(config)


@app.activity_trigger(input_name="config")
def apply_provider_flags_activity(config: dict) -> dict:
    return apply_provider_flags_fn(config)


# ── (CountyEnrichment Orchestrator + Activities retired 2026-05-28) ──────────
# All CountyEnrichment-* and pass{1,2,3,4,6}-* orchestrator/activity triggers
# have been removed. They existed only to expose CountyEnrichment as a
# standalone pipeline (anti-pattern: standalone enrichment pipeline with no
# business value on its own) and to back the OLD ProviderPipeline that
# delegated its enrichment phase out to those external orchestrations
# (violation of the "provider pipeline owns the providers collection
# end-to-end with no external orchestrations" principle).
#
# The provider pipeline (provider_pipeline_orchestrator) is the one and only
# provider pipeline and does enrichment inline inside
# process_assignment_activity.
#
# Helper utilities still in county_enrichment_job.py — _get_crosswalk,
# _get_maps_county_lookup, _build_states_filter, PROVIDERS_COLLECTION,
# _build_enrichment_reconcile — are not Durable triggers and remain
# importable by narrow internal callers (StampEmbeddingVersion, etc.).


# ═══════════════════════════════════════════════════════════════════════════════
# PRESCRIBER PIPELINE — CMS Part D + OIG LEIE + SAM.gov → provider_quality
# ═══════════════════════════════════════════════════════════════════════════════

@app.orchestration_trigger(context_name="context")
def prescriber_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    """Prescriber behavior pipeline — sequential. See PRESCRIBER_PIPELINE_STEPS
    for the canonical ordering.

    `steps` (list[str], optional): canonical step labels to run. Defaults to
    every label except the validate-only short-circuit. Reserve and release
    are NOT in `steps` — they always run.

    `start_step` is not used here (use `steps` to skip).
    """
    import datetime as dt

    config = context.get_input() or {}
    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    cluster_name = config.get("cluster_name", "ChatHealthyDataPipelines")

    default_steps = [
        PRESCRIBER_LABEL_FETCH,
        PRESCRIBER_LABEL_LOAD,
        PRESCRIBER_LABEL_CROSSWALK,
        PRESCRIBER_LABEL_ENRICH,
        PRESCRIBER_LABEL_SPECIALTY,
        PRESCRIBER_LABEL_EMBED,
    ]
    steps = config.get("steps", default_steps)

    results = {}

    # Validate is a short-circuit step — runs alone returns immediately.
    if PRESCRIBER_LABEL_VALIDATE in steps:
        context.set_custom_status(PRESCRIBER_LABEL_VALIDATE)
        results["validate"] = yield context.call_activity(
            "prescriber_validate_activity",
            {"env_prefix": env_prefix, "states": states, "steps": steps})
        if steps == [PRESCRIBER_LABEL_VALIDATE]:
            return results

    reservation = {
        "job_id": f"prescriber_{env_prefix}_{int(time.time())}",
        "requester": "PrescriberPipeline",
        "cluster_name": cluster_name,
        "expected_duration_minutes": config.get("expected_duration_minutes", 120),
    }
    context.set_custom_status(PRESCRIBER_LABEL_RESERVE)
    yield context.call_activity("register_reservation_activity", reservation)

    deadline = context.current_utc_datetime + dt.timedelta(minutes=30)
    while context.current_utc_datetime < deadline:
        status = yield context.call_activity("check_cluster_state_activity",
                                              {"cluster_name": cluster_name})
        if status.get("cluster_state") == "IDLE":
            break
        next_check = context.current_utc_datetime + dt.timedelta(seconds=15)
        yield context.create_timer(next_check)

    pipeline_error = None
    try:
        if PRESCRIBER_LABEL_FETCH in steps:
            context.set_custom_status(PRESCRIBER_LABEL_FETCH)
            results["fetch"] = yield context.call_activity(
                "prescriber_fetch_activity",
                {"env_prefix": env_prefix})

        if PRESCRIBER_LABEL_LOAD in steps:
            context.set_custom_status(PRESCRIBER_LABEL_LOAD)
            results["load"] = yield context.call_activity(
                "prescriber_load_activity",
                {"env_prefix": env_prefix, "states": states})

        if PRESCRIBER_LABEL_CROSSWALK in steps:
            context.set_custom_status(PRESCRIBER_LABEL_CROSSWALK)
            results["crosswalk"] = yield context.call_activity(
                "prescriber_crosswalk_activity",
                {"env_prefix": env_prefix, "states": states})

        if PRESCRIBER_LABEL_ENRICH in steps:
            context.set_custom_status(PRESCRIBER_LABEL_ENRICH)
            results["enrich"] = yield context.call_activity(
                "prescriber_enrich_activity",
                {"env_prefix": env_prefix, "states": states})

        if PRESCRIBER_LABEL_SPECIALTY in steps:
            context.set_custom_status(PRESCRIBER_LABEL_SPECIALTY)
            results["specialty"] = yield context.call_activity(
                "prescriber_specialty_activity",
                {"env_prefix": env_prefix, "states": states})

        if PRESCRIBER_LABEL_EMBED in steps:
            context.set_custom_status(PRESCRIBER_LABEL_EMBED)
            results["embed"] = yield context.call_activity(
                "prescriber_embed_activity",
                {"env_prefix": env_prefix, "states": states})

    except Exception as exc:
        pipeline_error = str(exc)
        logging.error("PrescriberPipeline FAILED: %s", exc)
        context.set_custom_status(f"FAILED — {pipeline_error[:100]}")

    finally:
        context.set_custom_status(PRESCRIBER_LABEL_RELEASE)
        yield context.call_activity("release_reservation_activity", reservation)

    if pipeline_error:
        results["error"] = pipeline_error

    return results


@app.activity_trigger(input_name="config")
def prescriber_validate_activity(config: dict) -> dict:
    """Step 0: Validate — check preconditions, return collection counts."""
    from pipeline_db import get_db
    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    db = get_db(env_prefix)
    state_filter = {"addresses.state": {"$in": states}}
    return {
        "status": "valid",
        "pipeline": "PrescriberEvaluateCarePipeline",
        "env_prefix": env_prefix,
        "states": states,
        "steps_requested": config.get("steps", []),
        "collections": {
            "providers": db["providers"].count_documents(state_filter),
            "provider_quality": db["provider_quality"].count_documents({}),
            "drug_indication_cache": db["drug_indication_cache"].count_documents({}),
        },
    }


@app.activity_trigger(input_name="config")
def prescriber_fetch_activity(config: dict) -> dict:
    """Step 1: Download CMS Part D + OIG LEIE + SAM.gov to blob storage."""
    from prescriber_data_fetcher import fetch_all
    return fetch_all(config)


@app.activity_trigger(input_name="config")
def prescriber_load_activity(config: dict) -> dict:
    """Step 2: Load CMS Part D into provider_quality — left outer join from providers."""
    from prescriber_load_worker import PrescriberLoadWorker
    worker = PrescriberLoadWorker({
        "env_prefix": config.get("env_prefix", "dev"),
        "states": config.get("states", ["DE"]),
        "batch_size": 500,
        "report_collection": config.get("report_collection", "admin.PipelineDiscrepancyReports"),
    })
    return worker.pipeline_execute()


@app.activity_trigger(input_name="config")
def prescriber_crosswalk_activity(config: dict) -> dict:
    """Step 3: Crosswalk enrichment — molecule→indication→ICD-10 via RxNorm + UMLS."""
    from crosswalk_builder import enrich_providers_with_crosswalk
    return enrich_providers_with_crosswalk(
        env_prefix=config.get("env_prefix", "dev"),
        states=config.get("states", ["DE"]),
    )


@app.activity_trigger(input_name="config")
def prescriber_enrich_activity(config: dict) -> dict:
    """Step 4: Exclusion flags — OIG LEIE + SAM.gov."""
    from prescriber_enrichment_job import enrich_all
    return enrich_all(
        env_prefix=config.get("env_prefix", "dev"),
        states=config.get("states", ["DE"]),
        batch_size=100,
    )


@app.activity_trigger(input_name="config")
def prescriber_specialty_activity(config: dict) -> dict:
    """Step 5: Specialty normalization — peer benchmarks and cost measure bands."""
    from crosswalk_builder import compute_specialty_baselines
    return compute_specialty_baselines(
        env_prefix=config.get("env_prefix", "dev"),
        states=config.get("states", ["DE"]),
    )


@app.activity_trigger(input_name="config")
def prescriber_embed_activity(config: dict) -> dict:
    """Step 6: Embed — vector embeddings for prescriber drug/molecule search.
    Uses text-embedding-3-large (3072d) — same model as all other embeddings
    so prescriber data can be RAG'd together with provider data."""
    import os
    from pymongo import UpdateOne
    from openai import OpenAI
    from pipeline_db import get_db
    from embedding_worker import EMBED_MODEL  # text-embedding-3-large

    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    db = get_db(env_prefix)
    quality_coll = db["provider_quality"]
    oai = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    cursor = quality_coll.find(
        {"measures.prescriber_behavior.drugs": {"$exists": True, "$ne": []}},
        {"npi": 1, "measures.prescriber_behavior.drugs": 1}
    )

    batch = []
    embedded = 0

    for doc in cursor:
        npi = doc["npi"]
        drugs = doc.get("measures", {}).get("prescriber_behavior", {}).get("drugs", [])

        parts = []
        for d in drugs:
            parts.append(d.get("molecule", ""))
            parts.extend(d.get("brand_names", []))
            parts.extend(d.get("generic_names", []))
            for ind in d.get("indications", []):
                parts.append(ind.get("indication", ""))

        text = " ".join(p for p in parts if p)
        if not text:
            continue
        text = text[:8000]

        try:
            resp = oai.embeddings.create(model=EMBED_MODEL, input=text)
            vector = resp.data[0].embedding

            batch.append(UpdateOne(
                {"npi": npi},
                {"$set": {
                    "prescriber_embedding": vector,
                    "prescriber_embedding_text": text[:500],
                    "prescriber_embedding_model": EMBED_MODEL,
                }}
            ))
            embedded += 1

            if len(batch) >= 50:
                quality_coll.bulk_write(batch, ordered=False)
                logging.info("Prescriber embed: %d NPIs", embedded)
                batch = []

        except Exception as e:
            logging.warning("Prescriber embedding failed for NPI %s: %s", npi, e)

    if batch:
        quality_coll.bulk_write(batch, ordered=False)

    logging.info("Prescriber embedding complete: %d NPIs, model=%s", embedded, EMBED_MODEL)
    return {"status": "complete", "npis_embedded": embedded, "model": EMBED_MODEL}
