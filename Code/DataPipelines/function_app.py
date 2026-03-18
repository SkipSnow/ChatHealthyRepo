import json
import logging
from pathlib import Path

from dotenv import load_dotenv

# Load .env for local development only
_env_path = Path(__file__).resolve().parent / ".env"
if _env_path.exists():
    load_dotenv(_env_path)

import azure.functions as func

from auth import require_auth
from load_specialty_data import run_load_specialty_data

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

PIPELINE_ROUTE = "Router"

TASK_HANDLERS = {
    "LoadSpecialtyData": run_load_specialty_data,
}


def json_response(payload: dict, status_code: int) -> func.HttpResponse:
    return func.HttpResponse(
        body=json.dumps(payload),
        status_code=status_code,
        mimetype="application/json",
    )


@app.function_name(name="DevPipelineManagementService")
@app.route(route=PIPELINE_ROUTE)
def dev_pipeline_management(req: func.HttpRequest) -> func.HttpResponse:
    """Dev Pipeline Management Service - requires Bearer token, routes supported tasks."""
    try:
        user_id, err = require_auth(req)
        if err:
            status_code, message = err
            logging.warning("Authentication failed for route '%s': %s", PIPELINE_ROUTE, message)
            return func.HttpResponse(
                body=message,
                status_code=status_code,
                mimetype="text/plain",
            )

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
        logging.info("User '%s' requested task '%s'", user_id, task)

        handler = TASK_HANDLERS.get(task)
        if handler is None:
            return json_response(
                {"success": False, "error": f"Unknown task: {task}", "task": task},
                400,
            )

        result = handler(req_body.get("payload") or {})

        logging.info("Task '%s' completed successfully for user '%s'", task, user_id)
        return json_response({"success": True, "task": task, "data": result}, 200)

    except Exception:
        logging.exception("Unhandled error in DevPipelineManagementService")
        return func.HttpResponse(
            body="Internal server error",
            status_code=500,
            mimetype="text/plain",
        )
