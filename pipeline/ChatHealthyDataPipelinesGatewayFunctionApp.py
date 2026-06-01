# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ChatHealthyDataPipelinesGatewayFunctionApp — pure HTTP facade.

Routes by `ChatHealthyTask` to the data-pipeline implementation. Holds no
knowledge of pipeline internals. Each route is a self-contained contract:
required args, allowed args, static defaults, env-derived defaults. The
function app authenticates, validates per-route, applies defaults, mints a
request_guid, reads the build_id from MongoDB admin.Versions, logs init,
forwards via HTTPS to the durable function app, and returns the upstream
response verbatim. Adding/changing a pipeline arg = editing one ROUTES
entry; the durable function app is never touched.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import urllib.error
import urllib.request
import uuid

import azure.functions as func


DURABLE_ROUTER_URL = os.environ.get(
    "DURABLE_ROUTER_URL",
    "https://prod-providerpipeline-app.victoriousisland-72795dbc.eastus2.azurecontainerapps.io/api/Router",
)
_FORWARD_TIMEOUT_S = 60.0


# ──────────────────────────────────────────────────────────────────────────────
# Per-route contract table. ChatHealthyTask is the only globally-required key;
# everything else is per-route. Each route declares:
#   - required:  arg names that MUST be present
#   - allowed:   arg name → static default (None = no static default)
#   - computed:  arg name → f-string template applied with env_prefix when the
#                arg is missing and has no static default
# ──────────────────────────────────────────────────────────────────────────────
ROUTES: dict = {
    "ProviderPipeline": {
        "required": ["states", "expected_duration_minutes", "provider_collection"],
        "allowed": {
            "states":                    None,
            "batch_size":                1000,
            "crosswalk_collection":      None,
            "embed_model":               "text-embedding-3-large",
            "embedding_enabled":         False,
            "env_prefix":                "dev",
            "expected_duration_minutes": None,
            "google_maps_enabled":       False,
            "incremental":               False,
            "provider_collection":       None,
            "report_collection":         "admin.PipelineDiscrepancyReports",
            "start_step":                None,
        },
        "computed": {
            "crosswalk_collection": "{env}_PublicHealthData.ZipCountyCrosswalk",
        },
    },
    "SpecialtyPipeline": {
        "required": ["expected_duration_minutes"],
        "allowed": {
            "embed_model":               "text-embedding-3-large",
            "embedding_enabled":         False,
            "env_prefix":                "dev",
            "expected_duration_minutes": None,
            "report_collection":         "admin.PipelineDiscrepancyReports",
            "start_step":                None,
        },
        "computed": {},
    },
}


def require_auth(req) -> tuple[str | None, tuple[int, str] | None]:
    """Bearer-token auth. Tokens configured in API_TOKEN_MAP env var as
    a JSON object mapping {token: user_id}."""
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return None, (401, "Missing or invalid token. Use: Authorization: Bearer <token>")
    token = auth_header[7:].strip()
    if not token:
        return None, (401, "Missing or invalid token. Use: Authorization: Bearer <token>")
    try:
        token_map = json.loads(os.environ.get("API_TOKEN_MAP", "{}"))
    except json.JSONDecodeError:
        return None, (401, "Server token map is malformed")
    user_id = token_map.get(token)
    if not user_id:
        return None, (401, "Unknown token")
    return user_id, None


def _validate(body: dict, route: dict) -> tuple[bool, str | None]:
    args = {k: v for k, v in body.items() if k != "ChatHealthyTask"}
    unknown = sorted(k for k in args if k not in route["allowed"])
    if unknown:
        return False, f"Unknown args for this route: {unknown}"
    missing = [k for k in route["required"] if not args.get(k)]
    if missing:
        return False, f"Missing required args: {missing}"
    return True, None


def _apply_defaults(body: dict, route: dict) -> dict:
    out = {k: v for k, v in body.items() if k != "ChatHealthyTask"}
    for k, default in route["allowed"].items():
        if k not in out and default is not None:
            out[k] = default
    env_prefix = out.get("env_prefix") or route["allowed"].get("env_prefix") or "dev"
    out["env_prefix"] = env_prefix
    for k, template in route["computed"].items():
        if k not in out or out[k] is None:
            out[k] = template.format(env=env_prefix)
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
        env = os.environ.get("ENV_PREFIX", "dev")
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


def _forward(bearer: str, upstream_body: dict, request_guid: str) -> tuple[int, bytes, str]:
    headers = {
        "Authorization":  bearer,
        "Content-Type":   "application/json",
        "X-Request-Guid": request_guid,
    }
    req = urllib.request.Request(
        DURABLE_ROUTER_URL,
        data=json.dumps(upstream_body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_FORWARD_TIMEOUT_S) as resp:
            return resp.status, resp.read(), resp.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers.get("Content-Type", "application/json")


app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)


@app.function_name(name="ChatHealthyDataPipelinesGateway")
@app.route(route="Router")
def gateway_router(req: func.HttpRequest) -> func.HttpResponse:
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
        if not isinstance(body, dict):
            return _json({"success": False, "error": "Request body must be a JSON object"}, 400)

        task = body.get("ChatHealthyTask")
        if not isinstance(task, str) or not task.strip():
            return _json({"success": False, "error": "ChatHealthyTask is required"}, 400)
        task = task.strip()

        route = ROUTES.get(task)
        if route is None:
            return _json({"success": False, "error": f"Unknown route: {task!r}", "task": task}, 404)

        ok, err_msg = _validate(body, route)
        if not ok:
            return _json({"success": False, "error": err_msg, "task": task}, 400)

        args = _apply_defaults(body, route)
        request_guid = str(uuid.uuid4())
        build_id = _current_build_id()

        logging.info(json.dumps({
            "event":           "gateway_request",
            "build_id":        build_id,
            "request_guid":    request_guid,
            "user_id":         user_id,
            "ChatHealthyTask": task,
            "args":            args,
        }))

        upstream_body = {
            "ChatHealthyTask": task,
            "payload": {
                **args,
                "request_guid":    request_guid,
                "router_build_id": build_id,
            },
        }

        try:
            status_code, body_bytes, content_type = _forward(
                bearer=req.headers.get("Authorization", ""),
                upstream_body=upstream_body,
                request_guid=request_guid,
            )
        except (socket.timeout, urllib.error.URLError) as exc:
            logging.warning(
                "durable function app no-response  request_guid=%s err=%s",
                request_guid, exc,
            )
            return _json({
                "success":      False,
                "error":        "Resource not found (durable function app did not respond)",
                "request_guid": request_guid,
                "task":         task,
            }, 404)
        except Exception as exc:
            logging.exception("durable function app forward failed  request_guid=%s", request_guid)
            return _json({
                "success":      False,
                "error":        f"Forward failed: {exc!s}",
                "request_guid": request_guid,
                "task":         task,
            }, 502)

        durable_instance_id = None
        try:
            up_json = json.loads(body_bytes)
            if isinstance(up_json, dict):
                durable_instance_id = up_json.get("id")
        except Exception:
            pass

        logging.info(json.dumps({
            "event":               "gateway_response",
            "build_id":            build_id,
            "request_guid":        request_guid,
            "upstream_status":     status_code,
            "durable_instance_id": durable_instance_id,
        }))

        return func.HttpResponse(
            body=body_bytes,
            status_code=status_code,
            mimetype=content_type,
        )

    except Exception:
        logging.exception("Unhandled error in ChatHealthyDataPipelinesGateway")
        return func.HttpResponse(body="Internal server error", status_code=500, mimetype="text/plain")
