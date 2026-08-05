from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline DB - shared MongoDB access for all pipeline workers.
# Both clusters are provided through ChatHealthyMongoUtilities so every
# MongoClient in the pipeline flows through the canonical utility.
# ENV_PREFIX routing - no hardcoded database names (Framework 1.1).
# Environment values constrained by CV-010 (controlled_vocabularies.json).

import os

from pymongo import MongoClient

_log = ChatHealthyLoggingService()

# CV-010: environment controlled vocabulary
_VALID_ENVIRONMENTS = {"local", "dev", "qa", "prod"}


def _validate_env(env_prefix: str) -> str:
    """Validate env_prefix against CV-010."""
    if env_prefix not in _VALID_ENVIRONMENTS:
        raise ChatHealthyException(
            mode="value_error",
            message=(
                f"Invalid environment '{env_prefix}'. Must be one of "
                f"CV-010 values: {sorted(_VALID_ENVIRONMENTS)}"
            ),
        )
    return env_prefix


def get_mongo() -> MongoClient:
    """Return the pipeline-cluster MongoClient via ChatHealthyMongoUtilities.
    Uses pipelineEditor identity with X.509 mTLS authentication."""
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor")


def get_frontend_mongo() -> MongoClient:
    """Return the front-cluster MongoClient via ChatHealthyMongoUtilities.
    Uses pipelineEditor identity (unified identity) with X.509 mTLS authentication.
    Used by ClusterLifecycleManager so reservation reads/writes never
    depend on the pipeline cluster being awake."""
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor")


def get_db(env_prefix: str = None):
    """Get the PublicHealthData database with ENV_PREFIX routing.
    Environment constrained by CV-010."""
    env_prefix = env_prefix or os.environ.get("ENV_PREFIX", "dev")
    _validate_env(env_prefix)
    return get_mongo()[f"{env_prefix}_PublicHealthData"]
