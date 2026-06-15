# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical MongoClient provider for ChatHealthy.ai front-end services.

Realizes EPIC-003-F-004-S-001. Direct MongoClient(...) instantiation in
any ChatHealthy.ai-authored Python file outside THIS file is forbidden
and enforced at pre-commit via Rule-004.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from bson.json_util import dumps as bson_dumps
from pymongo import MongoClient

from .exceptions import ChatHealthyException
from .logging_service import ChatHealthyLoggingService

log = ChatHealthyLoggingService()
TIMEOUT_MS = 120000

cached_client: Optional[MongoClient] = None


def q(value: Any) -> str:
    """Render the verbatim JSON the application is asking pymongo to send."""
    try:
        return bson_dumps(value, default=str)
    except Exception:
        return repr(value)


class TimedCursor:
    """Pass-through cursor wrapper. Logs START / END / FAIL for the
    cursor iteration. Elapsed time is the asctime delta between START
    and END/FAIL — read it directly from the log; no time math here."""

    def __init__(self, cursor: Any, op: str, db: str, coll: str) -> None:
        self._cursor = cursor
        self._op = op
        self._db = db
        self._coll = coll

    def __iter__(self):
        log.info("mongo.%s iter START db=%s coll=%s",
                  self._op, self._db, self._coll)
        try:
            for doc in self._cursor:
                yield doc
            log.info("mongo.%s iter END db=%s coll=%s",
                      self._op, self._db, self._coll)
        except Exception as exc:
            log.info("mongo.%s iter FAIL db=%s coll=%s exc=%s: %s",
                      self._op, self._db, self._coll,
                      type(exc).__name__, exc)
            try:
                self._cursor.close()
            except Exception:
                pass
            raise

    def close(self) -> None:
        """Explicit close — delegate to the wrapped pymongo cursor.
        Surfaced so callers can use `with closing(coll.find(...))` and
        guarantee the server-side cursor is released even when the
        caller never iterates."""
        try:
            self._cursor.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._cursor, name)


class TimedCollection:
    """Pass-through Collection wrapper. Each op logs START with the
    verbatim args sent to pymongo, and END or FAIL on completion. The
    duration is the asctime delta between START and END/FAIL."""

    def __init__(self, coll: Any) -> None:
        self._coll = coll
        self._db_name = coll.database.name
        self._coll_name = coll.name

    def __getattr__(self, name: str) -> Any:
        return getattr(self._coll, name)

    def aggregate(self, pipeline, *args, **kwargs):
        log.info("mongo.aggregate START db=%s coll=%s pipeline=%s opts=%s",
                  self._db_name, self._coll_name, q(pipeline), q(kwargs))
        try:
            cursor = self._coll.aggregate(pipeline, *args, **kwargs)
        except Exception as exc:
            log.info("mongo.aggregate FAIL db=%s coll=%s exc=%s: %s",
                      self._db_name, self._coll_name,
                      type(exc).__name__, exc)
            raise
        return TimedCursor(cursor, "aggregate", self._db_name, self._coll_name)

    def find(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        proj = args[1] if len(args) > 1 else kwargs.get("projection")
        log.info("mongo.find START db=%s coll=%s filter=%s projection=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(proj), q(kwargs))
        try:
            cursor = self._coll.find(*args, **kwargs)
        except Exception as exc:
            log.info("mongo.find FAIL db=%s coll=%s exc=%s: %s",
                      self._db_name, self._coll_name,
                      type(exc).__name__, exc)
            raise
        return TimedCursor(cursor, "find", self._db_name, self._coll_name)

    def find_one(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        proj = args[1] if len(args) > 1 else kwargs.get("projection")
        log.info("mongo.find_one START db=%s coll=%s filter=%s projection=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(proj), q(kwargs))
        try:
            result = self._coll.find_one(*args, **kwargs)
            log.info("mongo.find_one END db=%s coll=%s hit=%s",
                      self._db_name, self._coll_name, result is not None)
            return result
        except Exception as exc:
            log.info("mongo.find_one FAIL db=%s coll=%s exc=%s: %s",
                      self._db_name, self._coll_name,
                      type(exc).__name__, exc)
            raise

    def count_documents(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        log.info("mongo.count_documents START db=%s coll=%s filter=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(kwargs))
        try:
            result = self._coll.count_documents(*args, **kwargs)
            log.info("mongo.count_documents END db=%s coll=%s n=%d",
                      self._db_name, self._coll_name, result)
            return result
        except Exception as exc:
            log.info("mongo.count_documents FAIL db=%s coll=%s exc=%s: %s",
                      self._db_name, self._coll_name,
                      type(exc).__name__, exc)
            raise


class TimedDatabase:
    def __init__(self, db: Any) -> None:
        self._db = db

    def __getitem__(self, name: str) -> TimedCollection:
        return TimedCollection(self._db[name])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


class TimedClient:
    """Pass-through proxy over MongoClient that wraps every Collection
    returned via `client[db][coll]` so each op logs START + END/FAIL via
    ChatHealthyLoggingService. No time math — the asctime in each log
    line is the only timestamp."""

    def __init__(self, client: MongoClient) -> None:
        self._client = client

    def __getitem__(self, name: str) -> TimedDatabase:
        return TimedDatabase(self._client[name])

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


class ChatHealthyMongoUtilities:
    """Canonical Mongo client provider.

    REQ-B-002: zero-parameter constructor; coordinates and service identity
    come from runtime environment configuration.
    REQ-B-003: process-wide singleton; repeated instantiation returns the
    same underlying MongoClient and pool.
    REQ-B-004: getConnection() is the single public method.
    REQ-B-005: timeoutMS set per CSOT, default 120000ms.
    REQ-B-008: logging governed by EPIC-008-F-011-S-005.
    """

    def __init__(self) -> None:
        global cached_client
        if cached_client is not None:
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

        kwargs = {"timeoutMS": TIMEOUT_MS}
        appname = os.environ.get("CHATHEALTHY_SERVICE_NAME")
        if appname:
            kwargs["appname"] = appname

        cached_client = MongoClient(uri, **kwargs)
        log.info(
            "MongoClient established (appname=%s timeoutMS=%d)",
            appname or "<unset>", TIMEOUT_MS,
        )

    def getConnection(self) -> TimedClient:
        return TimedClient(cached_client)
