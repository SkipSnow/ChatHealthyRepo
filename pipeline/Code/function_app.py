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
from load_specialty_data import run_load_specialty_data
from icd10_loader import load_icd10
from count_providers_by_state import count_providers_by_state
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
    findcare_pipeline_orchestrator_fn,
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
    "CheckMongoHealth": check_mongo_health,
    "StampEmbeddingVersion": stamp_embedding_version_fn,
    "CountProvidersByState": count_providers_by_state,
}

# Ops Manager tasks — infrastructure only, no pipeline business logic
def _get_mongo_conn():
    return os.environ.get("MONGO_connectionString", "")

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
    "CountyEnrichment": "county_enrichment_orchestrator",
    "FindCarePipeline": "findcare_pipeline_orchestrator",
    "SnapshotCollection": "snapshot_collection_orchestrator",
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
    "FindCarePipeline": {
        "valid_steps": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9],
        "step_names": {0: "reserve", 1: "health_check", 2: "specialty_metadata", 3: "load", 4: "county_pass1", 5: "county_pass2", 6: "county_pass3", 7: "county_pass4_maps", 8: "county_pass6_nppes", 9: "embed"},
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

    Checks overdue reservations (alerts human).
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
def findcare_pipeline_orchestrator(context: df.DurableOrchestrationContext):
    return findcare_pipeline_orchestrator_fn(context)


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
    return mgr.status(config.get("cluster_name", "ChatHealthyDataPipelines"))


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
def load_specialty_data_activity(config: dict) -> dict:
    """Load SpecialtyMetaData from NUCC CSV → blob → pipeline MongoDB."""
    return run_load_specialty_data(config)


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
    """Prescriber behavior pipeline — 6 steps, sequential.
    Step 1: Fetch CMS Part D + OIG LEIE + SAM.gov → Blob Storage
    Step 2: Load → provider_quality (left outer join from providers)
    Step 3: Crosswalk — molecule→indication→ICD-10 (RxNorm + UMLS)
    Step 4: Enrich — OIG/SAM exclusion flags
    Step 5: Specialty normalization — peer benchmarks + cost bands
    Step 6: Embed — vector embeddings (text-embedding-3-large)

    Cluster lifecycle: reserve → poll IDLE → try work → finally release.
    """
    import datetime as dt

    config = context.get_input() or {}
    env_prefix = config.get("env_prefix", "dev")
    states = config.get("states", ["DE"])
    steps = config.get("steps", [1, 2, 3, 4, 5, 6])
    cluster_name = config.get("cluster_name", "ChatHealthyDataPipelines")

    results = {}

    # Step 0: Validate — check preconditions, return status, don't execute
    if 0 in steps:
        results["validate"] = yield context.call_activity(
            "prescriber_validate_activity",
            {"env_prefix": env_prefix, "states": states, "steps": steps})
        if steps == [0]:
            return results

    # Reserve cluster (wakes it if paused)
    reservation = {
        "job_id": f"prescriber_{env_prefix}_{int(time.time())}",
        "requester": "PrescriberPipeline",
        "cluster_name": cluster_name,
        "expected_duration_minutes": config.get("expected_duration_minutes", 120),
    }
    context.set_custom_status("Reserving cluster")
    yield context.call_activity("register_reservation_activity", reservation)

    # Poll until cluster is IDLE
    deadline = context.current_utc_datetime + dt.timedelta(minutes=30)
    while context.current_utc_datetime < deadline:
        context.set_custom_status("Waiting for cluster IDLE")
        status = yield context.call_activity("check_cluster_state_activity",
                                              {"cluster_name": cluster_name})
        if status.get("cluster_state") == "IDLE":
            break
        next_check = context.current_utc_datetime + dt.timedelta(seconds=15)
        yield context.create_timer(next_check)

    # try/finally guarantees reservation release on success or failure
    pipeline_error = None
    try:
        # Step 1: Fetch
        if 1 in steps:
            context.set_custom_status("Step 1: Fetch")
            results["fetch"] = yield context.call_activity(
                "prescriber_fetch_activity",
                {"env_prefix": env_prefix})

        # Step 2: Load
        if 2 in steps:
            context.set_custom_status("Step 2: Load")
            results["load"] = yield context.call_activity(
                "prescriber_load_activity",
                {"env_prefix": env_prefix, "states": states})

        # Step 3: Crosswalk — molecule→indication→ICD-10 (RxNorm + UMLS)
        if 3 in steps:
            context.set_custom_status("Step 3: Crosswalk")
            results["crosswalk"] = yield context.call_activity(
                "prescriber_crosswalk_activity",
                {"env_prefix": env_prefix, "states": states})

        # Step 4: Enrich — OIG/SAM exclusion flags
        if 4 in steps:
            context.set_custom_status("Step 4: Exclusion flags")
            results["enrich"] = yield context.call_activity(
                "prescriber_enrich_activity",
                {"env_prefix": env_prefix, "states": states})

        # Step 5: Specialty normalization — peer benchmarks
        if 5 in steps:
            context.set_custom_status("Step 5: Specialty baselines")
            results["specialty"] = yield context.call_activity(
                "prescriber_specialty_activity",
                {"env_prefix": env_prefix, "states": states})

        # Step 6: Embed — vector embeddings (text-embedding-3-large)
        if 6 in steps:
            context.set_custom_status("Step 6: Embed")
            results["embed"] = yield context.call_activity(
                "prescriber_embed_activity",
                {"env_prefix": env_prefix, "states": states})

    except Exception as exc:
        pipeline_error = str(exc)
        logging.error("PrescriberPipeline FAILED: %s", exc)
        context.set_custom_status(f"FAILED — {pipeline_error[:100]}")

    finally:
        # ALWAYS release reservation — cluster pauses when last reservation drops
        context.set_custom_status("Releasing cluster")
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
