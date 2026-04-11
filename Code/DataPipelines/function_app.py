# Copyright © 2026 Skip Snow. All rights reserved.
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
from otp_manager import exchange_otp
from load_specialty_data import run_load_specialty_data
from icd10_loader import load_icd10
from sync_gateway_agent import run_promote_to_frontend
from copy_to_frontend import (run_copy_to_frontend, snapshot_collection_fn, create_frontend_vector_index_fn,
                              partition_source, copy_chunk, drop_destination,
                              migrate_small_collections, migrate_chunk, verify_parity,
                              copy_providers_only)
from promote_data_fn import run_promote_data
from gpt_reader import handle_gpt_reader
from pipeline_health import check_mongo_health
# from idle_monitor import check_and_pause  # disabled — see idle_monitor_timer below
from county_enrichment_job import (
    county_enrichment_orchestrator_fn,
    county_enrichment_pass1_orchestrator_fn,
    county_enrichment_pass2_orchestrator_fn,
    county_enrichment_pass3_orchestrator_fn,
    county_enrichment_pass4_orchestrator_fn,
    county_enrichment_pass6_nppes_orchestrator_fn,
    enrich_by_address_batch_fn,
    enrich_by_billing_batch_fn,
    enrich_by_maps_batch_fn,
    enrich_by_nppes_batch_fn,
    enrich_by_zip_batch_fn,
    enrichment_report_fn,
    get_billing_retryable_fn,
    get_distinct_zips_fn,
    get_maps_retryable_fn,
    get_nppes_retryable_fn,
    get_unenriched_fn,
    lookup_crosswalk_fn,
    mark_out_of_scope_fn,
    mark_zip_state_mismatch_fn,
    reset_geocoder_failed_fn,
)
from instance_warmer import cool_instances_fn, warm_instances_fn
from provider_load_manager import (
    create_vector_index_fn,
    download_zip_fn,
    drain_staging_fn,
    embed_worker_fn,
    ensure_preload_indexes_fn,
    ensure_postload_indexes_fn,
    extract_csv_fn,
    full_provider_pipeline_orchestrator_fn,
    partition_file_fn,
    provider_load_orchestrator_fn,
    provider_worker_fn,
    reconcile_fn,
    register_reservation_fn,
    release_reservation_fn,
    report_fn,
    stamp_embedding_version_fn,
    write_metadata_fn,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

PIPELINE_ROUTE = "Router"

# Synchronous tasks — called directly, return 200
SYNC_TASK_HANDLERS = {
    "LoadSpecialtyData": run_load_specialty_data,
    "LoadICD10": load_icd10,
    "CreateFrontendVectorIndex": create_frontend_vector_index_fn,
    "CheckMongoHealth": check_mongo_health,
    "StampEmbeddingVersion": stamp_embedding_version_fn,
    "PromoteData": run_promote_data,
    "CopyToFrontEndSync": run_copy_to_frontend,
    "CopyProvidersOnly": copy_providers_only,
    "PromoteToFrontEnd": run_promote_to_frontend,
    "VerifyParity": verify_parity,
}

# Ops Manager tasks — infrastructure only, no pipeline business logic
def _get_mongo_conn():
    return os.environ.get("MONGO_FRONTEND_connectionString",
                          os.environ.get("MONGO_connectionString", ""))

def _get_ops_manager():
    from cluster_lifecycle_manager import ClusterLifecycleManager
    from pymongo import MongoClient
    conn = _get_mongo_conn()
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
        get_db_fn=lambda: MongoClient(conn),
        env_prefix=os.environ.get("ENV_PREFIX", "dev"),
        push_fn=push_fn,
    )

def _get_ops_agent():
    """Get the OpsManagerAgent — full agent with tools, triage, audit."""
    from cluster_lifecycle_manager import ClusterLifecycleManager
    from ops_manager import OpsManagerAgent
    from pymongo import MongoClient
    conn = _get_mongo_conn()
    env_prefix = os.environ.get("ENV_PREFIX", "dev")
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
        get_db_fn=lambda: MongoClient(conn),
        env_prefix=env_prefix,
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

# Asynchronous tasks — start a Durable orchestrator, return 202 + status URL
ASYNC_TASK_ORCHESTRATORS = {
    "LoadProviderData": "provider_load_orchestrator",
    "CountyEnrichment": "county_enrichment_orchestrator",
    "FullProviderPipeline": "full_provider_pipeline_orchestrator",
    "SnapshotCollection": "snapshot_collection_orchestrator",
    "CopyToFrontEnd": "copy_to_frontend_orchestrator",
    "MigrateEnvironment": "migrate_environment_orchestrator",
    "PrescriberEvaluateCarePipeline": "prescriber_pipeline_orchestrator",
}

# Pipeline step registry — valid steps per pipeline, with preconditions.
# Used by Router to validate steps[] before dispatching.
PIPELINE_STEP_REGISTRY = {
    "PrescriberEvaluateCarePipeline": {
        "valid_steps": [0, 1, 2, 3, 4],
        "step_names": {0: "validate", 1: "fetch", 2: "load", 3: "enrich", 4: "embed"},
        "preconditions": {
            2: {"requires_collection": "providers", "min_docs": 1, "note": "Step 2 builds from providers — need provider data loaded first"},
            3: {"requires_collection": "provider_quality", "min_docs": 1, "note": "Step 3 requires provider_quality records from step 2"},
            4: {"requires_collection": "provider_quality", "min_docs": 1, "note": "Step 4 requires enriched provider_quality from step 3"},
        },
    },
    "FullProviderPipeline": {
        "valid_steps": [0, 1, 2, 3, 4, 5, 6],
        "step_names": {0: "validate", 1: "download", 2: "load", 3: "county_pass1", 4: "county_pass2", 5: "county_pass3", 6: "embed"},
        "preconditions": {},
    },
}


def _validate_pipeline_steps(task: str, payload: dict) -> tuple:
    """Validate steps[] array against pipeline registry. Returns (valid, error_response)."""
    registry = PIPELINE_STEP_REGISTRY.get(task)
    if not registry:
        return True, None  # No registry entry — skip validation (legacy pipelines)

    steps = payload.get("steps")
    if steps is None:
        return True, None  # No steps specified — run all (default behavior)

    if not isinstance(steps, list):
        return False, json_response({
            "error": "InvalidStepError",
            "message": "steps must be an array of integers",
            "task": task,
        }, 400)

    valid_steps = registry["valid_steps"]
    for s in steps:
        if s not in valid_steps:
            return False, json_response({
                "error": "InvalidStepError",
                "message": f"Step {s} does not exist in {task}. Valid steps: {valid_steps}",
                "valid_steps": {str(k): v for k, v in registry["step_names"].items()},
                "task": task,
            }, 400)

    # Check preconditions for requested steps
    env_prefix = payload.get("env_prefix", "dev")
    for s in steps:
        precond = registry.get("preconditions", {}).get(s)
        if precond and precond.get("min_docs", 0) > 0:
            try:
                from pipeline_db import get_db
                coll_name = precond["requires_collection"]
                count = get_db(env_prefix)[coll_name].count_documents({}, limit=1)
                if count < precond["min_docs"]:
                    return False, json_response({
                        "error": "PreconditionError",
                        "message": f"Step {s} ({registry['step_names'].get(s, '?')}) requires {coll_name} to have records. {precond.get('note', '')}",
                        "task": task,
                    }, 400)
            except Exception as e:
                logging.warning("Precondition check failed for step %d: %s", s, e)

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


# ── GPT Reader — Read-only broker for GPT ────────────────────────────────────

@app.function_name(name="GPTReader")
@app.route(route="GPTReader", methods=["POST"])
def gpt_reader_route(req: func.HttpRequest) -> func.HttpResponse:
    """Read-only query service for GPT. Separate auth from Router (R4)."""
    try:
        # Extract Bearer token from Authorization header
        auth_header = req.headers.get("Authorization", "")
        token = auth_header.replace("Bearer ", "").strip() if auth_header.startswith("Bearer ") else ""

        try:
            config = req.get_json()
        except ValueError:
            return json_response({"error": "Request body must be valid JSON"}, 400)

        status_code, response = handle_gpt_reader(config, token)
        return json_response(response, status_code)

    except Exception:
        logging.exception("Unhandled error in GPTReader")
        return func.HttpResponse(body="Internal server error", status_code=500, mimetype="text/plain")


# ── OTP Key Exchange ──────────────────────────────────────────────────────────

@app.function_name(name="ExchangeOTP")
@app.route(route="ExchangeOTP", methods=["GET"])
def exchange_otp_route(req: func.HttpRequest) -> func.HttpResponse:
    """
    Exchange a one-time password for a permanent Brain API Bearer key.

    GET /api/ExchangeOTP?code=XXXX-XXXX

    Returns 200 + {"bearer_token": "...", "agent": "..."} on success.
    Returns 401 on invalid/expired/used OTP.
    OTP is consumed on first use. Expires after 30 minutes.
    """
    code = req.params.get("code", "").strip().upper()
    if not code:
        return json_response({"error": "Missing ?code= parameter"}, 400)

    success, agent, bearer_key = exchange_otp(code)

    if not success:
        logging.warning(f"[ExchangeOTP] Failed exchange — reason: {agent}")
        return json_response({"error": agent}, 401)

    logging.info(f"[ExchangeOTP] Key issued to agent: {agent}")
    return json_response({
        "bearer_token": bearer_key,
        "agent": agent,
        "message": "OTP accepted. Use this bearer_token in all Brain API calls: Authorization: Bearer <token>",
    }, 200)


# ── Idle Monitor (Timer Trigger) ──────────────────────────────────────────────

# Idle monitor disabled — paused cluster mid-run (fix: pipeline lock needed before re-enabling)
# @app.timer_trigger(schedule="0 */30 * * * *", arg_name="myTimer", run_on_startup=False)
# def idle_monitor_timer(myTimer: func.TimerRequest) -> None:
#     """Auto-pause ChatHealthyDataPipelines if idle longer than IDLE_MONITOR_THRESHOLD_HOURS."""
#     check_and_pause()


# ── Cluster Lifecycle Manager — hourly check for overdue reservations ─────────

@app.timer_trigger(schedule="0 */5 * * * *", arg_name="myTimer", run_on_startup=False)
def cluster_lifecycle_timer(myTimer: func.TimerRequest) -> None:
    """Ops-only timer. No task execution. No pipeline imports.

    Checks overdue reservations (alerts Boss).
    Shuts down idle clusters (zero reservations).
    Checks for stuck clusters.
    Uses OpsManagerAgent for full triage + audit trail.
    """
    try:
        agent = _get_ops_agent()
        cluster_name = os.environ.get("PIPELINE_CLUSTER", "ChatHealthyDataPipelines")
        result = agent.handle_event({
            "type": "timer_check",
            "cluster_name": cluster_name,
        })
        if result.success:
            data = result.data or {}
            logging.info("Ops timer: %s, %d reservations",
                         data.get("cluster_state", "?"), data.get("active_reservations", 0))
        else:
            logging.warning("Ops timer returned error: %s", result.error_message)
    except Exception:
        logging.exception("Cluster lifecycle timer failed")


# ── Durable Orchestrators ─────────────────────────────────────────────────────

@app.orchestration_trigger(context_name="context")
def full_provider_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    return full_provider_pipeline_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def provider_load_orchestrator(context: df.DurableOrchestrationContext):
    return provider_load_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def copy_to_frontend_orchestrator(context: df.DurableOrchestrationContext):
    """Async CopyToFrontEnd — count, partition, fan out workers by _id range.

    Pattern: reserve → poll → static collections → drop destination →
             partition source → fan out N workers (one per chunk) →
             vector index → release.

    Each worker copies a fixed-size _id range. Every worker is identical.
    No special cases for first/last. Job number tracks each worker.

    payload:
        env_prefix             — "dev" (default)
        cluster_name           — "ChatHealthyDataPipelines" (default)
        chunk_size             — records per worker (default: 50,000)
        expected_duration_minutes — reservation duration (default: 120)
    """
    import datetime as dt

    config = context.get_input() or {}
    cluster_name = config.get("cluster_name", "ChatHealthyDataPipelines")
    env_prefix = config.get("env_prefix", "dev")
    chunk_size = config.get("chunk_size", 50_000)
    job_id = config.get("job_id", f"copy_to_frontend_{int(time.time())}")

    # Build state filter for provider query — only copy requested states
    states = config.get("states")
    if isinstance(states, list) and states:
        provider_query = {"practice_address.state": {"$in": states}}
    else:
        provider_query = {}

    # Step 1: Reserve cluster
    reservation = {
        "job_id": job_id,
        "requester": "CopyToFrontEnd",
        "cluster_name": cluster_name,
        "expected_duration_minutes": config.get("expected_duration_minutes", 120),
    }
    context.set_custom_status("Reserving cluster")
    yield context.call_activity("register_reservation_activity", reservation)

    # BUG-PIPE-002: try/finally guarantees reservation release on any failure
    pipeline_error = None
    total_copied = 0
    results = []
    parity = {}
    chunks = []
    try:
        # Step 2: Wait for cluster IDLE
        deadline = context.current_utc_datetime + dt.timedelta(minutes=30)
        while context.current_utc_datetime < deadline:
            context.set_custom_status("Waiting for cluster IDLE")
            status = yield context.call_activity("check_cluster_state_activity",
                                                  {"cluster_name": cluster_name})
            if status.get("cluster_state") == "IDLE":
                break
            next_check = context.current_utc_datetime + dt.timedelta(seconds=15)
            yield context.create_timer(next_check)

        # Step 3: Copy static collections
        context.set_custom_status("Copying static collections")
        yield context.call_activity("copy_to_frontend_activity", {
            "env_prefix": env_prefix,
            "states": [],
        })

        # Step 4: Drop destination providers (clean slate)
        context.set_custom_status("Dropping destination providers")
        yield context.call_activity("drop_destination_activity", {
            "env_prefix": env_prefix,
            "collection": "providers",
        })

        # Step 5: Partition source into chunks by _id range
        context.set_custom_status(f"Partitioning source (states: {states or 'all'})")
        partition = yield context.call_activity("partition_source_activity", {
            "env_prefix": env_prefix,
            "collection": "providers",
            "chunk_size": chunk_size,
            "query": provider_query,
        })
        chunks = partition.get("chunks", [])
        total = partition.get("total", 0)

        # Step 5.5: Pre-warm instances so each worker gets its own container
        if chunks:
            context.set_custom_status(f"Pre-warming {len(chunks)} instances")
            yield context.call_activity("warm_instances_activity", {
                "num_instances": min(len(chunks), 10),
            })

        # Step 6: Fan out — parallel workers, one per chunk
        context.set_custom_status(f"Copying {total:,} providers across {len(chunks)} workers")
        copy_tasks = []
        for chunk in chunks:
            copy_tasks.append(context.call_activity("copy_chunk_activity", {
                "env_prefix": env_prefix,
                "collection": "providers",
                "job_number": chunk["job_number"],
                "start_id": chunk["start_id"],
                "end_id": chunk["end_id"],
            }))
        results = yield context.task_all(copy_tasks)
        total_copied = sum(r.get("copied", 0) for r in results)

        # Step 6.5: Cool down instances
        yield context.call_activity("cool_instances_activity", {})

        # Step 7: Create vector indexes (BUG-PIPE-008: BOTH provider AND specialty)
        context.set_custom_status("Creating vector search indexes")
        try:
            yield context.call_activity("create_frontend_vector_index_activity", {
                "env_prefix": env_prefix,
            })
        except Exception as idx_err:
            logging.error("Vector index creation failed: %s", idx_err)

        # Step 8: Parity verification
        context.set_custom_status("Verifying parity")
        parity = yield context.call_activity("verify_parity_activity", {
            "env_prefix": env_prefix,
            "states": states if isinstance(states, list) and states else None,
        })
        parity_pass = parity.get("all_pass", False)
        if not parity_pass:
            context.set_custom_status(f"PARITY FAILURE — {parity}")
            logging.error("CopyToFrontEnd parity check FAILED: %s", parity)

    except Exception as exc:
        pipeline_error = str(exc)
        logging.error("CopyToFrontEnd FAILED: %s", exc)
        context.set_custom_status(f"FAILED — {pipeline_error[:100]}")

    # Step 9: ALWAYS release reservation — success or failure (BUG-PIPE-002)
    context.set_custom_status("Releasing cluster")
    yield context.call_activity("release_reservation_activity", reservation)

    if pipeline_error:
        context.set_custom_status(f"FAILED — {pipeline_error[:100]}")
        return {"status": "failed", "error": pipeline_error, "total_copied": total_copied}

    context.set_custom_status(f"Done — {total_copied:,} providers in {len(chunks)} chunks")
    return {"status": "complete", "total_copied": total_copied, "chunks": len(chunks), "results": results, "parity": parity}


@app.orchestration_trigger(context_name="context")
def migrate_environment_orchestrator(context: df.DurableOrchestrationContext):
    """Migrate all PublicHealthData collections from one env to another.

    Small collections: $out (single activity, fast).
    Large collections (providers): partition by _id range, one chunk per activity.

    payload:
        src_env      — "dev" (default)
        dst_env      — "qa" (default)
        cluster_name — "ChatHealthyDataPipelines" (default)
        chunk_size   — records per chunk for large collections (default: 50,000)
    """
    import datetime as dt

    config = context.get_input() or {}
    src_env = config.get("src_env", "dev")
    dst_env = config.get("dst_env", "qa")
    chunk_size = config.get("chunk_size", 50_000)
    cluster_name = config.get("cluster_name", "ChatHealthyDataPipelines")

    # Reserve cluster
    reservation = {
        "job_id": f"migrate_{src_env}_to_{dst_env}_{int(time.time())}",
        "requester": "MigrateEnvironment",
        "cluster_name": cluster_name,
        "expected_duration_minutes": config.get("expected_duration_minutes", 120),
    }
    context.set_custom_status("Reserving cluster")
    yield context.call_activity("register_reservation_activity", reservation)

    # Wait for IDLE
    deadline = context.current_utc_datetime + dt.timedelta(minutes=30)
    while context.current_utc_datetime < deadline:
        context.set_custom_status("Waiting for cluster IDLE")
        status = yield context.call_activity("check_cluster_state_activity",
                                              {"cluster_name": cluster_name})
        if status.get("cluster_state") == "IDLE":
            break
        next_check = context.current_utc_datetime + dt.timedelta(seconds=15)
        yield context.create_timer(next_check)

    # Step 1: Migrate small collections via $out
    context.set_custom_status(f"Migrating small collections {src_env} → {dst_env}")
    small_result = yield context.call_activity("migrate_small_collections_activity", {
        "src_env": src_env,
        "dst_env": dst_env,
    })

    # Step 2: Chunked copy for providers
    context.set_custom_status("Partitioning providers")
    src_db_name = f"{src_env}_PublicHealthData"
    dst_db_name = f"{dst_env}_PublicHealthData"

    partition = yield context.call_activity("partition_source_activity", {
        "env_prefix": src_env,
        "collection": "providers",
        "chunk_size": chunk_size,
    })
    chunks = partition.get("chunks", [])
    total = partition.get("total", 0)

    # Drop destination providers before copying
    context.set_custom_status("Dropping destination providers")
    yield context.call_activity("drop_destination_activity", {
        "env_prefix": dst_env,
        "collection": "providers",
        "use_pipeline_cluster": True,
    })

    # Fan out — one chunk at a time
    total_copied = 0
    for chunk in chunks:
        jn = chunk["job_number"]
        context.set_custom_status(f"Providers {jn+1}/{len(chunks)} — {total_copied:,}/{total:,}")
        r = yield context.call_activity("migrate_chunk_activity", {
            "src_env": src_env,
            "dst_env": dst_env,
            "collection": "providers",
            "job_number": jn,
            "start_id": chunk["start_id"],
            "end_id": chunk["end_id"],
        })
        total_copied += r.get("copied", 0)

    # Release
    context.set_custom_status("Releasing cluster")
    yield context.call_activity("release_reservation_activity", reservation)

    context.set_custom_status(f"Done — {total_copied:,} providers + small collections")
    return {
        "status": "complete",
        "src": src_db_name,
        "dst": dst_db_name,
        "providers_copied": total_copied,
        "chunks": len(chunks),
        "small_collections": small_result,
    }


@app.orchestration_trigger(context_name="context")
def snapshot_collection_orchestrator(context: df.DurableOrchestrationContext):
    config = context.get_input() or {}
    src = config.get("source", "providers")
    dst = config.get("destination", "?")
    context.set_custom_status(f"Copying {src} → {dst}")
    result = yield context.call_activity("snapshot_collection_activity", config)
    context.set_custom_status(f"Done — {result.get('copied', 0):,} docs in {dst}")
    return result


# ── Durable Activities ────────────────────────────────────────────────────────

@app.activity_trigger(input_name="config")
def check_mongo_health_activity(config: dict) -> dict:
    return check_mongo_health(config)


@app.activity_trigger(input_name="config")
def copy_to_frontend_activity(config: dict) -> dict:
    return run_copy_to_frontend(config)


@app.activity_trigger(input_name="config")
def migrate_small_collections_activity(config: dict) -> dict:
    return migrate_small_collections(config)


@app.activity_trigger(input_name="config")
def migrate_chunk_activity(config: dict) -> dict:
    return migrate_chunk(config)


@app.activity_trigger(input_name="config")
def drop_destination_activity(config: dict) -> dict:
    return drop_destination(config)


@app.activity_trigger(input_name="config")
def partition_source_activity(config: dict) -> dict:
    return partition_source(config)


@app.activity_trigger(input_name="config")
def copy_chunk_activity(config: dict) -> dict:
    return copy_chunk(config)


@app.activity_trigger(input_name="config")
def verify_parity_activity(config: dict) -> dict:
    return verify_parity(config)


@app.activity_trigger(input_name="config")
def create_frontend_vector_index_activity(config: dict) -> dict:
    return create_frontend_vector_index_fn(config)


@app.activity_trigger(input_name="config")
def check_cluster_state_activity(config: dict) -> dict:
    """Return cluster state for orchestrator polling."""
    mgr = _get_ops_manager()
    return mgr.status(config.get("cluster_name", "ChatHealthyDataPipelines"))


@app.activity_trigger(input_name="config")
def snapshot_collection_activity(config: dict) -> dict:
    return snapshot_collection_fn(config)


@app.activity_trigger(input_name="config")
def download_zip_activity(config: dict) -> str:
    return download_zip_fn(config)


@app.activity_trigger(input_name="config")
def extract_csv_activity(config: dict) -> str:
    return extract_csv_fn(config)


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
def write_metadata_activity(config: dict) -> list:
    return write_metadata_fn(config)


@app.activity_trigger(input_name="config")
def register_reservation_activity(config: dict) -> dict:
    return register_reservation_fn(config)


@app.activity_trigger(input_name="config")
def release_reservation_activity(config: dict) -> dict:
    return release_reservation_fn(config)


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


# ── County Enrichment Orchestrator + Activities ───────────────────────────────

@app.orchestration_trigger(context_name="context")
def county_enrichment_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def county_enrichment_pass1_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_pass1_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def county_enrichment_pass2_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_pass2_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def county_enrichment_pass3_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_pass3_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def county_enrichment_pass4_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_pass4_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def county_enrichment_pass6_nppes_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_pass6_nppes_orchestrator_fn(context)


@app.activity_trigger(input_name="config")
def get_distinct_zips_activity(config: dict) -> dict:
    return get_distinct_zips_fn(config)


@app.activity_trigger(input_name="config")
def lookup_crosswalk_activity(config: dict) -> dict:
    return lookup_crosswalk_fn(config)


@app.activity_trigger(input_name="config")
def enrich_by_zip_batch_activity(config: dict) -> dict:
    return enrich_by_zip_batch_fn(config)


@app.activity_trigger(input_name="config")
def get_unenriched_activity(config: dict) -> dict:
    return get_unenriched_fn(config)


@app.activity_trigger(input_name="config")
def enrich_by_address_batch_activity(config: dict) -> dict:
    return enrich_by_address_batch_fn(config)


@app.activity_trigger(input_name="config")
def reset_geocoder_failed_activity(config: dict) -> dict:
    return reset_geocoder_failed_fn(config)


@app.activity_trigger(input_name="config")
def mark_out_of_scope_activity(config: dict) -> dict:
    return mark_out_of_scope_fn(config)


@app.activity_trigger(input_name="config")
def mark_zip_state_mismatch_activity(config: dict) -> dict:
    return mark_zip_state_mismatch_fn(config)


@app.activity_trigger(input_name="config")
def get_billing_retryable_activity(config: dict) -> dict:
    return get_billing_retryable_fn(config)


@app.activity_trigger(input_name="config")
def enrich_by_billing_batch_activity(config: dict) -> dict:
    return enrich_by_billing_batch_fn(config)


@app.activity_trigger(input_name="config")
def get_maps_retryable_activity(config: dict) -> dict:
    return get_maps_retryable_fn(config)


@app.activity_trigger(input_name="config")
def enrich_by_maps_batch_activity(config: dict) -> dict:
    return enrich_by_maps_batch_fn(config)


@app.activity_trigger(input_name="config")
def get_nppes_retryable_activity(config: dict) -> dict:
    return get_nppes_retryable_fn(config)


@app.activity_trigger(input_name="config")
def enrich_by_nppes_batch_activity(config: dict) -> dict:
    return enrich_by_nppes_batch_fn(config)


@app.activity_trigger(input_name="config")
def enrichment_report_activity(config: dict) -> dict:
    return enrichment_report_fn(config)


# ═══════════════════════════════════════════════════════════════════════════════
# PRESCRIBER PIPELINE — CMS Part D + OIG LEIE + SAM.gov → provider_quality
# ═══════════════════════════════════════════════════════════════════════════════

@app.orchestration_trigger(context_name="context")
def prescriber_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    """Prescriber behavior pipeline — 3 steps, sequential.
    Step 1: Fetch CMS Part D + OIG LEIE + SAM.gov → Blob Storage
    Step 2: Load → provider_quality (left outer join from providers)
    Step 3: Enrich → drug indications (LLM), exclusion flags, location
    """
    config = context.get_input() or {}
    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    steps = config.get("steps", [1, 2, 3])

    results = {}

    # Step 0: Validate — check preconditions, return status, don't execute
    if 0 in steps:
        results["validate"] = yield context.call_activity(
            "prescriber_validate_activity",
            {"env_prefix": env_prefix, "states": states, "steps": steps})
        if steps == [0]:
            return results

    # Step 1: Fetch
    if 1 in steps:
        results["fetch"] = yield context.call_activity(
            "prescriber_fetch_activity",
            {"env_prefix": env_prefix})

    # Step 2: Load
    if 2 in steps:
        results["load"] = yield context.call_activity(
            "prescriber_load_activity",
            {"env_prefix": env_prefix, "states": states})

    # Step 3: Enrich
    if 3 in steps:
        results["enrich"] = yield context.call_activity(
            "prescriber_enrich_activity",
            {"env_prefix": env_prefix, "states": states})

    return results


@app.activity_trigger(input_name="config")
def prescriber_validate_activity(config: dict) -> dict:
    """Step 0: Validate — check preconditions, return collection counts."""
    from pipeline_db import get_db
    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    db = get_db(env_prefix)
    state_filter = {"practice_address.state": {"$in": states}}
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
    })
    return worker.pipeline_execute()


@app.activity_trigger(input_name="config")
def prescriber_enrich_activity(config: dict) -> dict:
    """Step 3: Enrich — drug indications, exclusion flags, location, taxonomy."""
    from prescriber_enrichment_job import enrich_all
    return enrich_all(
        env_prefix=config.get("env_prefix", "dev"),
        states=config.get("states", ["DE"]),
        batch_size=100,
    )
