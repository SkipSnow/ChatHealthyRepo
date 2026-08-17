from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService,
    set_mongo_log_identity,
)
from chathealthy_lib.exceptions import ChatHealthyException
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline DB - shared MongoDB access for all pipeline workers.
# Both clusters are provided through ChatHealthyMongoUtilities so every
# MongoClient in the pipeline flows through the canonical utility.
# ENV_PREFIX routing - no hardcoded database names (Framework 1.1).
# Environment values constrained by CV-010 (controlled_vocabularies.json).



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


# Every piece of pipeline metadata -- configuration, discrepancy reports, run
# counters, fatal records, load state -- lives in this one database and nowhere
# else. Physical pipeline DATA (provider collections, staging) is separate and
# lives on the pipelines target. Named once, by the report service every
# pipeline shares.


