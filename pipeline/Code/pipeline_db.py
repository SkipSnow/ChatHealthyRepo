# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline DB — shared MongoDB access for all pipeline workers.
# Module-level singleton (correct pattern for Azure Functions).
# ENV_PREFIX routing — no hardcoded database names (Framework 1.1).
# Environment values constrained by CV-010 (controlled_vocabularies.json).

import os
import logging

from pymongo import MongoClient

_log = logging.getLogger("pipeline_db")

_mongo: MongoClient | None = None
_frontend_mongo: MongoClient | None = None

# CV-010: environment controlled vocabulary
_VALID_ENVIRONMENTS = {"local", "dev", "qa", "prod"}


def _validate_env(env_prefix: str) -> str:
    """Validate env_prefix against CV-010. Raises ValueError if invalid."""
    if env_prefix not in _VALID_ENVIRONMENTS:
        raise ValueError(
            f"Invalid environment '{env_prefix}'. "
            f"Must be one of CV-010 values: {sorted(_VALID_ENVIRONMENTS)}"
        )
    return env_prefix


def get_mongo() -> MongoClient:
    """Get the shared MongoClient singleton for pipeline workers."""
    global _mongo
    if _mongo is None:
        conn_str = os.environ.get("MONGO_connectionString")
        if not conn_str:
            raise RuntimeError("MONGO_connectionString not set")
        # serverSelectionTimeoutMS=120_000 — survive Atlas autoscale elections
        # (~30-90s); matches the timeout the provider load worker has used.
        _mongo = MongoClient(conn_str, serverSelectionTimeoutMS=120_000)
        _log.info("MongoDB connected (serverSelectionTimeoutMS=120s)")
    return _mongo


def get_frontend_mongo() -> MongoClient:
    """Get the shared MongoClient singleton for the always-on front-end cluster.

    Used by ClusterLifecycleManager so reservation reads/writes never depend
    on the pipeline cluster being awake.
    """
    global _frontend_mongo
    if _frontend_mongo is None:
        conn_str = os.environ.get("MONGO_FRONTEND_connectionString")
        if not conn_str:
            raise RuntimeError("MONGO_FRONTEND_connectionString not set")
        _frontend_mongo = MongoClient(conn_str, serverSelectionTimeoutMS=120_000)
        _log.info("MongoDB front-end connected (serverSelectionTimeoutMS=120s)")
    return _frontend_mongo


def get_db(env_prefix: str = None):
    """Get the PublicHealthData database with ENV_PREFIX routing.
    Environment constrained by CV-010."""
    env_prefix = env_prefix or os.environ.get("ENV_PREFIX", "dev")
    _validate_env(env_prefix)
    return get_mongo()[f"{env_prefix}_PublicHealthData"]

