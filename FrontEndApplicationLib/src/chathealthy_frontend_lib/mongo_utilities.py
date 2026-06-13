# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical MongoClient provider for ChatHealthy.ai front-end services.

Realizes EPIC-003-F-004-S-001. Direct MongoClient(...) instantiation in
any ChatHealthy.ai-authored Python file outside THIS file is forbidden
and enforced at pre-commit via Rule-004.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from pymongo import MongoClient

from .exceptions import ChatHealthyException

_log = logging.getLogger(__name__)

_TIMEOUT_MS = 15000

_client: Optional[MongoClient] = None


class ChatHealthyMongoUtilities:
    """Canonical Mongo client provider.

    REQ-B-002: zero-parameter constructor; coordinates and service identity
    come from runtime environment configuration.
    REQ-B-003: process-wide singleton; repeated instantiation returns the
    same underlying MongoClient and pool.
    REQ-B-004: getConnection() is the single public method.
    REQ-B-005: timeoutMS set per CSOT, default 15000ms.
    REQ-B-008: logging governed by EPIC-008-F-011-S-005.
    """

    def __init__(self) -> None:
        global _client
        if _client is not None:
            return

        uri = os.environ.get("MONGO_FRONTEND_connectionString")
        if not uri:
            raise ChatHealthyException(
                mode="mongo_env_unset",
                message=(
                    "MONGO_FRONTEND_connectionString not set in the runtime "
                    "environment; ChatHealthyMongoUtilities cannot connect."
                ),
                component="ChatHealthyMongoUtilities",
            )

        kwargs = {"timeoutMS": _TIMEOUT_MS}
        appname = os.environ.get("CHATHEALTHY_SERVICE_NAME")
        if appname:
            kwargs["appname"] = appname

        _client = MongoClient(uri, **kwargs)
        _log.info(
            "MongoClient established (appname=%s timeoutMS=%d)",
            appname or "<unset>", _TIMEOUT_MS,
        )

    def getConnection(self) -> MongoClient:
        return _client
