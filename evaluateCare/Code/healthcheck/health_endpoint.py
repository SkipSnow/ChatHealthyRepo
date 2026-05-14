# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os
import logging


class HealthEndpoint:
    """GET /health — env, db status, build/version/framework from admin.Versions.

    Failure semantics (NO silent fallbacks):
      - Mongo URI not configured       → status: "degraded"
      - Mongo connection/read fails    → status: "degraded"
      - build/version/framework are
        explicit None when the read
        didn't succeed, never the
        misleading sentinel "?".
    """

    def __init__(self):
        self.log = logging.getLogger("evaluate_care.health")
        self.env_prefix = os.getenv("ENV_PREFIX", "dev")
        self.uri = os.environ.get("MONGO_FRONTEND_connectionString")

    def __call__(self):
        if not self.uri:
            return {
                "status": "degraded",
                "service": "evaluate_care",
                "db": "unconfigured",
                "env": self.env_prefix,
                "build": None, "version": None, "framework": None,
                "error": "MONGO_FRONTEND_connectionString env var not set",
            }
        try:
            from pymongo import MongoClient
            client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
            doc = client["admin"]["Versions"].find_one(sort=[("from", -1)]) or {}
        except Exception as e:
            self.log.warning("/health Mongo read failed: %s", e)
            return {
                "status": "degraded",
                "service": "evaluate_care",
                "db": "unreachable",
                "env": self.env_prefix,
                "build": None, "version": None, "framework": None,
                "error": f"{type(e).__name__}: {e}",
            }
        # Per-env builds[] array shape: extract this container's env slot.
        _builds_arr = doc.get("builds", [])
        _build_for_env = None
        if isinstance(_builds_arr, list):
            _build_for_env = next(
                (b.get("build") for b in _builds_arr
                 if isinstance(b, dict) and b.get("env") == self.env_prefix),
                None,
            )
        return {
            "status": "ok",
            "service": "evaluate_care",
            "db": "connected",
            "env": self.env_prefix,
            "build": _build_for_env,
            "version": doc.get("version"),
            "framework": doc.get("framework"),
        }
