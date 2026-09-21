# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FindCare configuration and the front-end database handle.

Drained out of app.py so the tools that own the mining and the search can
reach the same environment binding and the same lazy Mongo handle without
importing the FastAPI application module. app.py collects and hands off; it
does not own the database handle or the environment.
"""
from __future__ import annotations

import os

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

log = ChatHealthyLoggingService()

ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")

_db_manager = None


def get_db():
    """The front-end cluster handle, lazily acquired through the canonical
    utility. Returns None when Mongo is momentarily unreachable so the next
    call retries via the same lazy path."""
    global _db_manager
    try:
        if _db_manager is None:
            _db_manager = ChatHealthyMongoUtilities()
        return _db_manager.getConnection("frontendUser", "ChatHealthyFrontEnd")
    except Exception as e:  # noqa: BLE001 - recoverable; caller retries
        # Mode 1 (REQ-B-008): recoverable -- the caller's next call retries
        # via the same lazy-init path. log.info so this only emits in debug.
        log.info("MongoDB unavailable (will retry next call): %s", e,
                 exc=ChatHealthyException(
                     mode="mongo_unavailable",
                     message=f"MongoDB unavailable (will retry next call): {e}",
                     component="FindCareBackend",
                     exception=e))
        _db_manager = None
        return None
