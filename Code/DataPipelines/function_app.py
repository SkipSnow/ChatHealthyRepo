import json
import os
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root (works regardless of working directory)
_env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(_env_path)

import azure.functions as func

from auth import require_auth

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)

PileLineRouter="Router"
@app.function_name(name="DevPipelineManagmentService")
@app.route(route=PileLineRouter)
def dev_pipeline_management(req: func.HttpRequest) -> func.HttpResponse:
    """Dev Pipeline Management Service - requires Bearer token, returns greeting for authenticated user."""
    try:
        user_id, err = require_auth(req)
        if err:
            status_code, message = err
            return func.HttpResponse(
                body=message,
                status_code=status_code,
                mimetype="text/plain",
            )
        if req.method == "GET":
            return func.HttpResponse(
                body="This API only supports the HTTP Post method",
                status_code=405,
                mimetype="text/plain",
            )
        req_body = req.get_json(silent=True) or {}
        task = req_body.get("ChatHealthyTask", "not provided")
        body = json.dumps({
            "success": True,
            "message": f"Hello, {user_id}! Welcome to the secured pipeline.",
            "ChatHealthyTask": task,
        })
        return func.HttpResponse(
            body=body,
            status_code=200,
            mimetype="application/json",
        )
    except Exception as e:
        return func.HttpResponse(
            body=f"Error: {str(e)}",
            status_code=500,
            mimetype="text/plain",
        )
