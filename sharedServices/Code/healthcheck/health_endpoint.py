# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import json
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import os
from pathlib import Path

log = ChatHealthyLoggingService()


BUILD_INFO_PATH = "/app/build_info.json"


def read_build_info() -> dict | None:
    """Return the build_info.json baked into the image at build time, or
    None if the file is absent (older image; caller falls back to the
    frontEndAdmin.BuildVersions read for back-compat)."""
    p = Path(BUILD_INFO_PATH)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        # Mode 1 (REQ-B-008): caller falls back to placeholder build info.
        log.info(
            "build_info read failed (ignored, caller falls back): %s", exc,
            exc=ChatHealthyException(
                mode="build_info_read_failed",
                message=f"build_info read failed (ignored, caller falls back): {exc}",
                component="SharedServicesHealth",
                exception=exc,
            ),
        )
        return None


class HealthEndpoint:
    """POST /health — env, db status, build/version/framework.

    Source priority for build/version/framework:
      1. /app/build_info.json (baked at image build time — the truth about
         what's running)
      2. frontEndAdmin.BuildVersions latest doc (legacy fallback for older images)

    The db reachability check still runs so the db status field is honest.
    """

    def __init__(self):
        self.env_prefix = os.getenv("ENV_PREFIX", "dev")

    def __call__(self):
        baked = read_build_info()

        db_status = "unconfigured"
        mongo_doc: dict = {}
        try:
            from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
            client = ChatHealthyMongoUtilities().getConnection("frontendUser", "frontEnd")
            mongo_doc = client["frontEndAdmin"]["BuildVersions"].find_one(sort=[("from", -1)]) or {}
            db_status = "connected"
        except Exception as exc:
            # Mode 2 (REQ-B-008): Mongo read failed during /health
            # probe → db_status reported as "unreachable" upstream.
            # Operator must know.
            log.error(
                "/health Mongo read failed: %s", exc,
                exc=ChatHealthyException(
                    mode="health_mongo_read_failed",
                    message=f"/health Mongo read failed: {exc}",
                    component="SharedServicesHealth",
                    exception=exc,
                ),
                if_not_debug_log=True,
            )
            db_status = "unreachable"

        if baked is not None:
            return {
                "status": "ok" if db_status == "connected" else "degraded",
                "service": "shared_services",
                "db": db_status,
                "env": self.env_prefix,
                "build": baked.get("build"),
                "commit": baked.get("commit"),
                "built_at": baked.get("built_at"),
                "version": baked.get("version") or mongo_doc.get("version"),
                "git_number": baked.get("commit") or mongo_doc.get("git_number"),
                "source": "build_info.json",
            }

        return {
            "status": "ok" if db_status == "connected" else "degraded",
            "service": "shared_services",
            "db": db_status,
            "env": self.env_prefix,
            "build": mongo_doc.get("build"),
            "git_number": mongo_doc.get("git_number"),
            "version": mongo_doc.get("version"),
            "source": "frontEndAdmin.BuildVersions",
        }
