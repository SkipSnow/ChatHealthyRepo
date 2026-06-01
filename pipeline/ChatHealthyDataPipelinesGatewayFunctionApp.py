# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ChatHealthyDataPipelinesGatewayFunctionApp — single HTTP gateway for all
data-pipeline tasks.

Per EPIC-010-F-102-S-007-REQ-B-001: every pipeline enters through a single
authenticated URL. ChatHealthyTask names the orchestrator the Router
dispatches to. The Gateway never waits for the orchestration to complete —
it enqueues a NewExecutionStarted message into the shared Netherite task
hub and returns 202 with the standard Durable check-status payload (or an
error).

Owns:
  - HTTP Router (POST /api/Router) protected by Bearer-token auth
  - Payload allowlist validation (the 14 True-row keys from the spreadsheet)
  - request_guid generation + paired router_request / router_response logs
  - Durable client.start_new against the shared Netherite hub

Does NOT own:
  - Orchestrators / activities / entities (worker / ACA Container App)
  - Monitoring, warming, status — caller polls the returned status URL

Limits:
  - 3-minute HTTP timeout (host.json functionTimeout = 00:03:00)
  - client.start_new wrapped in asyncio.wait_for with a 60s ceiling so a
    Netherite cold-start cannot pin the Gateway up to the function timeout
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid

import azure.functions as func
import azure.durable_functions as df


def require_auth(req) -> tuple[str | None, tuple[int, str] | None]:
    """Bearer-token auth. Tokens are configured in the API_TOKEN_MAP env var
    as a JSON object mapping {token: user_id}. Returns (user_id, None) on
    success or (None, (status_code, message)) on failure."""
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, (401, "Missing or invalid token. Use: Authorization: Bearer <token>")
    token = auth_header[7:].strip()
    if not token:
        return None, (401, "Missing or invalid token. Use: Authorization: Bearer <token>")
    token_map_json = os.environ.get("API_TOKEN_MAP", "{}")
    try:
        token_map = json.loads(token_map_json)
    except json.JSONDecodeError:
        return None, (401, "Server token map is malformed")
    user_id = token_map.get(token)
    if not user_id:
        return None, (401, "Unknown token")
    return user_id, None


app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

PIPELINE_ROUTE = "Router"
_START_NEW_TIMEOUT_S = 60.0


TASK_ORCHESTRATORS: dict[str, str] = {
    "ProviderPipeline":               "provider_pipeline_orchestrator",
    "SpecialtyPipeline":              "specialty_pipeline_orchestrator",
    "SnapshotCollection":             "snapshot_collection_orchestrator",
    "PrescriberEvaluateCarePipeline": "prescriber_pipeline_orchestrator",
    "LoadSpecialtyData":              "load_specialty_data_orchestrator",
    "LoadICD10":                      "load_icd10_orchestrator",
    "CheckMongoHealth":               "check_mongo_health_orchestrator",
    "StampEmbeddingVersion":          "stamp_embedding_version_orchestrator",
    "CountProvidersByState":          "count_providers_by_state_orchestrator",
    "WakeCluster":                    "wake_cluster_orchestrator",
    "ClusterStatus":                  "cluster_status_orchestrator",
    "Release":                        "release_orchestrator",
    "ForceRelease":                   "force_release_orchestrator",
}


PAYLOAD_ALLOWLIST: dict = {
    "states":                    None,
    "expected_duration_minutes": 120,
    "incremental":               False,
    "batch_size":                1000,
    "embedding_enabled":         False,
    "google_maps_enabled":       False,
    "embed_model":               "text-embedding-3-large",
    "env_prefix":                None,
    "start_step":                None,
    "source_staleness":          None,
    "provider_collection":       None,
    "report_collection":         None,
    "crosswalk_collection":      None,
}


def _default_env_prefix() -> str:
    return os.environ.get("ENV_PREFIX", "dev")


def _validate_payload(payload: dict) -> tuple[bool, str | None, list]:
    unknown = sorted(k for k in payload.keys() if k not in PAYLOAD_ALLOWLIST)
    if unknown:
        return False, f"Unknown payload keys (allowlist violation): {unknown}", unknown
    return True, None, []


def _apply_defaults(payload: dict) -> dict:
    out = dict(payload)
    env_prefix = out.get("env_prefix") or _default_env_prefix()
    out["env_prefix"] = env_prefix
    for key, default in PAYLOAD_ALLOWLIST.items():
        if key == "env_prefix":
            continue
        if key in out:
            continue
        if default is not None:
            out[key] = default
    out.setdefault("provider_collection",  f"{env_prefix}_PublicHealthData.providers")
    out.setdefault("report_collection",    f"{env_prefix}_PublicHealthData.PipelineDiscrepencyReports")
    out.setdefault("crosswalk_collection", f"{env_prefix}_PublicHealthData.ZipCountyCrosswalk")
    return out


_versions_coll = None


def _versions_collection():
    global _versions_coll
    if _versions_coll is None:
        from pymongo import MongoClient
        conn = os.environ.get("MONGO_FRONTEND_connectionString")
        if not conn:
            raise RuntimeError("MONGO_FRONTEND_connectionString not set")
        _versions_coll = MongoClient(conn)["admin"]["Versions"]
    return _versions_coll


def _current_build_id() -> int | None:
    try:
        env = _default_env_prefix()
        latest = _versions_collection().find_one(sort=[("from", -1)])
        if latest is None:
            return None
        for entry in latest.get("builds", []):
            if entry.get("env") == env:
                return int(entry["build"])
        return None
    except Exception as exc:
        logging.warning("build_id lookup failed: %s", exc)
        return None


def _json(payload: dict, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


@app.function_name(name="ChatHealthyDataPipelinesGateway")
@app.route(route=PIPELINE_ROUTE)
@app.durable_client_input(client_name="client")
async def gateway_router(
    req: func.HttpRequest,
    client: df.DurableOrchestrationClient,
) -> func.HttpResponse:
    try:
        if req.method != "POST":
            return func.HttpResponse(body="POST only", status_code=405, mimetype="text/plain")

        user_id, err = require_auth(req)
        if err:
            status, message = err
            return func.HttpResponse(body=message, status_code=status, mimetype="text/plain")

        try:
            body = req.get_json()
        except ValueError:
            return _json({"success": False, "error": "Request body must be valid JSON"}, 400)

        task = body.get("ChatHealthyTask")
        if not isinstance(task, str) or not task.strip():
            return _json(
                {"success": False, "error": "ChatHealthyTask is required and must be a non-empty string"},
                400,
            )
        task = task.strip()
        if task not in TASK_ORCHESTRATORS:
            return _json(
                {"success": False, "error": f"Unknown ChatHealthyTask: {task!r}", "task": task},
                400,
            )

        payload = body.get("payload") or {}
        if not isinstance(payload, dict):
            return _json({"success": False, "error": "payload must be a JSON object", "task": task}, 400)

        ok, err_msg, unknown = _validate_payload(payload)
        if not ok:
            return _json(
                {"success": False, "error": err_msg, "unknown_keys": unknown, "task": task},
                400,
            )
        payload = _apply_defaults(payload)

        request_guid = str(uuid.uuid4())
        build_id = _current_build_id()

        logging.info(json.dumps({
            "event":           "router_request",
            "build_id":        build_id,
            "request_guid":    request_guid,
            "user_id":         user_id,
            "ChatHealthyTask": task,
            "payload":         payload,
        }))

        orchestrator_input = {
            **payload,
            "request_guid":    request_guid,
            "router_build_id": build_id,
        }
        orchestrator_name = TASK_ORCHESTRATORS[task]

        try:
            instance_id = await asyncio.wait_for(
                client.start_new(orchestrator_name, None, orchestrator_input),
                timeout=_START_NEW_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logging.warning(
                "client.start_new timed out after %ds for orchestrator=%s request_guid=%s",
                int(_START_NEW_TIMEOUT_S), orchestrator_name, request_guid,
            )
            return _json({
                "success":      False,
                "error":        f"start_new timed out after {int(_START_NEW_TIMEOUT_S)}s (Netherite likely cold-starting)",
                "request_guid": request_guid,
                "task":         task,
            }, 503)
        except Exception as exc:
            logging.exception(
                "client.start_new failed for orchestrator=%s request_guid=%s",
                orchestrator_name, request_guid,
            )
            return _json({
                "success":      False,
                "error":        f"start_new failed: {exc!s}",
                "request_guid": request_guid,
                "task":         task,
            }, 503)

        logging.info(json.dumps({
            "event":               "router_response",
            "build_id":            build_id,
            "request_guid":        request_guid,
            "durable_instance_id": instance_id,
        }))

        return client.create_check_status_response(req, instance_id)

    except Exception:
        logging.exception("Unhandled error in ChatHealthyDataPipelinesGateway")
        return func.HttpResponse(body="Internal server error", status_code=500, mimetype="text/plain")
