# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Report that a controller failed to come up.

Run by cloud-init from the same image as the controller, after a non-zero
docker exit. Nothing the failing container knew survives it: docker --rm
removes the container, and the cloud-init log lives on a host that is deleted
when the run ends. Without this, a controller that never started left the run
recorded as running, with no fatal error anywhere and nobody told.

Reached through bootstrap.py, so the vault secrets and the identity
certificate are already in place by the time this runs.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService, set_mongo_log_identity)

set_mongo_log_identity("pipelineEditor")
log = ChatHealthyLoggingService()

PIPELINE_ADMIN_DB = "pipelineAdmin"


def _detail() -> str:
    return (f"run_id={os.environ.get('RUN_ID', '<unset>')} "
            f"exit_code={os.environ.get('CONTROLLER_EXIT_CODE', '<unset>')} "
            f"env={os.environ.get('ENV_PREFIX', '<unset>')}")


def _mail(detail: str, mongo_error: str = "") -> None:
    from chathealthy_lib.notification_client import NotificationClient
    to = os.environ.get("NOTIFICATION_TO_EMAIL", "").strip()
    if not to:
        return
    lines = [
        "A pipeline controller failed to come up.",
        "",
        detail,
        "",
        "The container exited before it could record anything. It ran under "
        "docker --rm on a host that is deleted at the end of the run, so its "
        "own output no longer exists.",
    ]
    if mongo_error:
        lines += ["", f"Mongo was also unreachable: {mongo_error}"]
    NotificationClient().send_email(
        to=to,
        subject="ChatHealthy pipeline FATAL: controller failed to start",
        body=chr(10).join(lines))


def _mark_run_failed(exit_code: str, detail: str) -> None:
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    run_id = os.environ.get("RUN_ID", "").strip()
    if not run_id:
        return
    # pipelineAdmin lives on the front-end cluster. Naming "pipelines" here
    # would make reporting a failure depend on the one cluster a controller
    # must not touch before staging load -- the same mistake that made the
    # failure unreportable in the first place.
    db = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd")["pipelineAdmin"]
    db["pipeline.runs"].update_one(
        {"run_id": run_id, "status": "running"},
        {"$set": {"status": "failed",
                  "failure_reason": f"controller_exited_{exit_code}",
                  "failed_at": datetime.now(timezone.utc),
                  "failure_detail": detail}})


def main() -> int:
    detail = _detail()
    exit_code = os.environ.get("CONTROLLER_EXIT_CODE", "unknown")
    try:
        log.error("FATAL: controller failed to come up | %s", detail)
        _mark_run_failed(exit_code, detail)
    except Exception as exc:  # noqa: BLE001
        _mail(detail, f"{type(exc).__name__}: {exc}")
        return 1
    _mail(detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
