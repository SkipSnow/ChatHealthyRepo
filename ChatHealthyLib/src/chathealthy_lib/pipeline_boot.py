# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""AA runbook Mongo-logging bootstrap.

Every AA runbook that logs via ChatHealthyLoggingService with Mongo
destination must have CH_SPACE_NAME and ENV_PREFIX set BEFORE its first
log() call. Otherwise CHLS's _build_mongo_handler raises
'mongo_log_handler_env_unset' and the runbook aborts before any structured
event lands.

This module used to fetch a Mongo URI from Key Vault, translate it out of
SRV form over DNS-over-HTTPS, and place it in the environment for CHLS to
connect with. CHLS has not connected that way since the certificate model
landed -- its handler calls getConnection(identity, "admin") and the
identity's certificate is the credential. The fetch produced a working
database credential that nothing consumed and every runbook carried.

Usage from a runbook:

    from chathealthy_lib.pipeline_boot import bootstrap_aa_mongo_logging

    def main() -> int:
        bootstrap_aa_mongo_logging(component_name="watchdog")
        # ... rest of main; log() calls now write to Log_<env> Mongo.

This module is inlined into every AA runbook by the build chain
(build_chathealthy.py _inline_chathealthy_lib_if_used), so runbooks
never need to install any package beyond the AA sandbox baseline.
"""

from __future__ import annotations

import os


def bootstrap_aa_mongo_logging(
    component_name: str,
    env_prefix: str | None = None,
) -> None:
    """Set the environment ChatHealthyLoggingService needs for Mongo logging.

    MUST be called BEFORE the runbook's first log() call. Sets:
      * CH_SPACE_NAME (component_name)
      * ENV_PREFIX (arg or AUTOMATION_ENV_PREFIX or 'dev')
      * CH_COMPONENT (component_name)
      * CH_LOG_DESTINATION ('stderr,mongo' if not already set)

    No credential is fetched or placed. The handler authenticates with the
    logging identity's certificate through ChatHealthyMongoUtilities.
    """
    resolved_env = (
        env_prefix
        or os.environ.get("AUTOMATION_ENV_PREFIX")
        or "dev"
    )
    os.environ.setdefault("CH_SPACE_NAME", component_name)
    os.environ.setdefault("ENV_PREFIX", resolved_env)
    os.environ.setdefault("CH_COMPONENT", component_name)
    os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
