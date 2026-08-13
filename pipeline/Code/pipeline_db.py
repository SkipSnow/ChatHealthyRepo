from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService,
    set_mongo_log_identity,
)
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
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

# The pipeline acts as pipelineEditor, including when it writes its own logs.
# The Mongo log handler refuses to build without this, so it is set here:
# pipeline_db is imported by every pipeline component, which makes this the
# one place guaranteed to run before any pipeline code logs.
set_mongo_log_identity("pipelineEditor")

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


def get_mongo(cluster: str = "pipelines") -> MongoClient:
    """MongoClient for pipeline work, as pipelineEditor.

    Defaults to the `pipelines` target -- the data factory, which is down by
    design most of the day. Callers reaching for metadata want `admin`, and
    should use get_metadata_db() rather than passing it here.
    """
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor", cluster)


def get_frontend_mongo() -> MongoClient:
    """MongoClient for the frontEnd target, as pipelineEditor.

    Used by ClusterLifecycleManager. Note this returns the frontEnd target
    specifically so reservation reads and writes never depend on the pipeline
    factory being awake.
    """
    return ChatHealthyMongoUtilities().getConnection("pipelineEditor", "frontEnd")


def get_db(env_prefix: str = None):
    """Get the PipelinePublicHealthData database on the pipelines cluster.

    The pipeline's own published data. The name is deliberately distinct from
    the front end's PublicHealthData so that no accessor mistake can put
    pipeline output into the front-end application's database.

    Environments are separated by CLUSTER, not by database name. No database
    is named for an environment: there is no dev_, qa_ or prod_ prefix. The
    env_prefix argument is still validated so callers cannot pass a value
    outside CV-010, but it does not participate in the database name.
    """
    env_prefix = env_prefix or os.environ.get("ENV_PREFIX", "dev")
    _validate_env(env_prefix)
    return get_mongo("pipelines")["PipelinePublicHealthData"]


# Every piece of pipeline metadata -- configuration, discrepancy reports, run
# counters, fatal records, load state -- lives in this one database and nowhere
# else. Physical pipeline DATA (provider collections, staging) is separate and
# lives on the pipelines target.
PIPELINE_ADMIN_DB = "pipelineAdmin"


def get_metadata_db():
    """The single home for pipeline metadata.

    Lives on the `admin` target, which answers 24x7. Pipeline metadata must
    be readable while the data factory is asleep -- notably so that "factory
    unreachable" can itself be reported.
    """
    return get_mongo("admin")[PIPELINE_ADMIN_DB]
