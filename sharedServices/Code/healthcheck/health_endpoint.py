# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import json
import logging
import os
from pathlib import Path


_BUILD_INFO_PATH = "/app/build_info.json"


def _read_build_info() -> dict | None:
    """Return the build_info.json baked into the image at build time, or
    None if the file is absent (older image; caller falls back to the
    admin.Versions read for back-compat)."""
    p = Path(_BUILD_INFO_PATH)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


class HealthEndpoint:
    """POST /health — env, db status, build/version/framework.

    Source priority for build/version/framework:
      1. /app/build_info.json (baked at image build time — the truth about
         what's running)
      2. admin.Versions latest doc (legacy fallback for older images)

    The db reachability check still runs so the db status field is honest.
    """

    def __init__(self):
        self.log = logging.getLogger("shared_services.health")
        self.env_prefix = os.getenv("ENV_PREFIX", "dev")
        self.uri = os.environ.get("MONGO_FRONTEND_connectionString")

    def __call__(self):
        baked = _read_build_info()

        db_status = "unconfigured"
        mongo_doc: dict = {}
        if self.uri:
            try:
                from pymongo import MongoClient
                client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
                mongo_doc = client["admin"]["Versions"].find_one(sort=[("from", -1)]) or {}
                db_status = "connected"
            except Exception as e:
                self.log.warning("/health Mongo read failed: %s", e)
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
                "framework": baked.get("framework") or mongo_doc.get("framework"),
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
            "source": "admin.Versions",
        }
