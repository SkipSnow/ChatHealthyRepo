# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os
import logging


class HealthEndpoint:
    """GET /health — env, db status, build/version/framework from admin.Versions."""

    def __init__(self):
        self.log = logging.getLogger("evaluate_care.health")
        self.env_prefix = os.getenv("ENV_PREFIX", "dev")
        self.uri = os.environ.get("MONGO_FRONTEND_connectionString")

    def __call__(self):
        build = "?"; version = "?"; framework = "?"; db = "unavailable"
        if self.uri:
            try:
                from pymongo import MongoClient
                client = MongoClient(self.uri, serverSelectionTimeoutMS=5000)
                doc = client["admin"]["Versions"].find_one(sort=[("from", -1)]) or {}
                build = doc.get("build", "?")
                version = doc.get("version", "?")
                framework = doc.get("framework", "?")
                db = "connected"
            except Exception as e:
                self.log.warning("/health Mongo read failed: %s", e)
        return {
            "status": "ok",
            "service": "evaluate_care",
            "db": db,
            "env": self.env_prefix,
            "build": build,
            "version": version,
            "framework": framework,
        }
