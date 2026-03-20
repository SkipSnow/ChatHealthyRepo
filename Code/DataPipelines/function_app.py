# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

import json
import logging
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local development only (shared file lives one level up at Code/.env)
_env_path = Path(__file__).resolve().parent.parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

import azure.durable_functions as df
import azure.functions as func

from auth import require_auth
from load_specialty_data import run_load_specialty_data
from icd10_loader import load_icd10
from migrate_from_legacy import run_migrate_from_legacy
from copy_to_frontend import run_copy_to_frontend
from atlas_cluster_manager import scale_up, scale_down
from county_enrichment_job import (
    county_enrichment_orchestrator_fn,
    enrich_by_address_batch_fn,
    enrich_by_zip_batch_fn,
    enrichment_report_fn,
    get_distinct_zips_fn,
    get_unenriched_fn,
    lookup_crosswalk_fn,
)
from provider_load_manager import (
    county_enrich_fn,
    download_zip_fn,
    ensure_indexes_fn,
    extract_csv_fn,
    partition_file_fn,
    provider_load_orchestrator_fn,
    provider_worker_fn,
    reconcile_fn,
    report_fn,
    write_metadata_fn,
    worker_enrichment_pair_fn,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

PIPELINE_ROUTE = "Router"

# Synchronous tasks — called directly, return 200
SYNC_TASK_HANDLERS = {
    "LoadSpecialtyData": run_load_specialty_data,
    "LoadICD10": load_icd10,
    "MigrateFromLegacy": run_migrate_from_legacy,
    "CopyToFrontEnd": run_copy_to_frontend,
    "ScaleUp": lambda config: scale_up(config.get("cluster", "ChatHealthyDataPipelines")) or {"status": "scaled_up"},
    "ScaleDown": lambda config: scale_down(config.get("cluster", "ChatHealthyDataPipelines")) or {"status": "scaled_down"},
}

# Asynchronous tasks — start a Durable orchestrator, return 202 + status URL
ASYNC_TASK_ORCHESTRATORS = {
    "LoadProviderData": "provider_load_orchestrator",
    "CountyEnrichment": "county_enrichment_orchestrator",
}


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
        logging.info("User '%s' requested task '%s'", user_id, task)

        # Synchronous path
        if task in SYNC_TASK_HANDLERS:
            result = SYNC_TASK_HANDLERS[task](payload)
            logging.info("Task '%s' completed for user '%s'", task, user_id)
            return json_response({"success": True, "task": task, "data": result}, 200)

        # Asynchronous path — start Durable orchestrator, return 202
        if task in ASYNC_TASK_ORCHESTRATORS:
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


# ── Durable Orchestrators ─────────────────────────────────────────────────────

@app.orchestration_trigger(context_name="context")
def provider_load_orchestrator(context: df.DurableOrchestrationContext):
    return provider_load_orchestrator_fn(context)


@app.orchestration_trigger(context_name="context")
def worker_enrichment_pair(context: df.DurableOrchestrationContext):
    return worker_enrichment_pair_fn(context)


# ── Durable Activities ────────────────────────────────────────────────────────

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
def ensure_indexes_activity(config: dict) -> None:
    return ensure_indexes_fn(config)


@app.activity_trigger(input_name="config")
def write_metadata_activity(config: dict) -> list:
    return write_metadata_fn(config)


@app.activity_trigger(input_name="config")
def provider_worker_activity(config: dict) -> dict:
    return provider_worker_fn(config)


@app.activity_trigger(input_name="config")
def county_enrich_activity(config: dict) -> dict:
    return county_enrich_fn(config)


@app.activity_trigger(input_name="config")
def reconcile_activity(config: dict) -> dict:
    return reconcile_fn(config)


@app.activity_trigger(input_name="config")
def report_activity(config: dict) -> dict:
    return report_fn(config)


# ── County Enrichment Orchestrator + Activities ───────────────────────────────

@app.orchestration_trigger(context_name="context")
def county_enrichment_orchestrator(context: df.DurableOrchestrationContext):
    return county_enrichment_orchestrator_fn(context)


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
def enrichment_report_activity(config: dict) -> dict:
    return enrichment_report_fn(config)
