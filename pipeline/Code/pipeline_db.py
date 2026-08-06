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
    """Get the PublicHealthData database.

    Environments are separated by CLUSTER, not by database name. No database
    is named for an environment: there is no dev_, qa_ or prod_ prefix. The
    env_prefix argument is still validated so callers cannot pass a value
    outside CV-010, but it does not participate in the database name.
    """
    env_prefix = env_prefix or os.environ.get("ENV_PREFIX", "dev")
    _validate_env(env_prefix)
    return get_mongo()["PublicHealthData"]


# Every piece of pipeline metadata -- configuration, discrepancy reports, run
# counters, fatal records, load state -- lives in this one database and nowhere
# else. Physical pipeline DATA (provider collections, staging) is separate and
# stays in the env-prefixed PublicHealthData databases.
METADATA_DB = "Pipelines"


def get_metadata_db():
    """The single home for pipeline metadata: frontEnd.Pipelines."""
    return get_mongo()[METADATA_DB]
