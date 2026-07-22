# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical MongoClient provider for ChatHealthy.ai front-end services.

Realizes EPIC-008-F-002-S-010. Direct MongoClient(...) instantiation in
any ChatHealthy.ai-authored Python file outside THIS file is forbidden
and enforced at pre-commit via Rule-004.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from bson.json_util import dumps as bson_dumps
from pymongo import MongoClient
from pymongo.errors import (
    ConfigurationError,
    ConnectionFailure,
    DocumentTooLarge,
    EncryptionError,
    InvalidDocument,
    InvalidName,
    InvalidOperation,
    OperationFailure,
    ProtocolError,
    ServerSelectionTimeoutError,
)

from .exceptions import ChatHealthyException
from .logging_service import ChatHealthyLoggingService

log = ChatHealthyLoggingService()
TIMEOUT_MS = 120000

cached_client: Optional[MongoClient] = None
# Per-env-var cache. Pipeline code connects to multiple clusters
# (MONGO_FRONTEND_connectionString for the always-up front cluster,
# MONGO_connectionString for the pipeline cluster, MONGO_CLUSTER_<x>_
# connectionString for migration source/dest). One MongoClient
# singleton per distinct env-var name keeps the shared-pool invariant
# while serving multi-cluster callers.
_client_cache: dict[str, MongoClient] = {}


def q(value: Any) -> str:
    """Render the verbatim JSON the application is asking pymongo to send."""
    try:
        return bson_dumps(value, default=str)
    except Exception:
        return repr(value)


def _exc_detail(exc: BaseException) -> str:
    """Render verbatim the Mongo-side response carried on a pymongo exception:
    .code, .code_name, and .details (the raw server reply document)."""
    parts = []
    code = getattr(exc, "code", None)
    if code is not None:
        parts.append(f"code={code}")
    code_name = getattr(exc, "code_name", None)
    if code_name is not None:
        parts.append(f"code_name={code_name}")
    details = getattr(exc, "details", None)
    if details is not None:
        parts.append(f"details={q(details)}")
    errors = getattr(exc, "errors", None)
    if errors:
        parts.append(f"errors={q(errors)}")
    return " ".join(parts)


def _classify_mongo_exception(exc: BaseException, elapsed_s: float) -> str:
    """Map a pymongo error to a ChatHealthy mode. Empty string for non-pymongo
    exceptions — caller re-raises raw.

    `mongo_query_timeout` ONLY fires when elapsed is within 2s of the
    configured client budget (TIMEOUT_MS). Anything else from the
    ConnectionFailure family is `mongo_network_failure` — the connection
    layer dropped for some reason that is NOT a budget exhaustion."""
    if isinstance(exc, (ConnectionFailure, ServerSelectionTimeoutError)):
        if elapsed_s >= (TIMEOUT_MS / 1000.0) - 2.0:
            return "mongo_query_timeout"
        return "mongo_network_failure"
    if isinstance(exc, DocumentTooLarge):
        return "mongo_document_too_large"
    if isinstance(exc, OperationFailure):
        return "mongo_server_rejected"
    if isinstance(exc, (ConfigurationError, InvalidOperation, InvalidName, InvalidDocument)):
        return "mongo_invalid_operation"
    if isinstance(exc, (ProtocolError, EncryptionError)):
        return "mongo_protocol_failure"
    return ""


def _convert_mongo_exception(exc: BaseException, elapsed_s: float,
                              op: str, db: str, coll: str) -> None:
    """Translate a recognised pymongo exception into a ChatHealthyException
    carrying the verbatim Mongo response in context, raised `from exc`. For
    an unrecognised exception, return None — the caller re-raises raw."""
    mode = _classify_mongo_exception(exc, elapsed_s)
    if not mode:
        return
    raise ChatHealthyException(
        mode=mode,
        message=f"mongo.{op} {db}.{coll} {type(exc).__name__}: {exc}",
        component="ChatHealthyMongoUtilities",
        elapsed_s=round(elapsed_s, 3),
        mongo_detail=_exc_detail(exc),
        op=op,
        db=db,
        coll=coll,
    ) from exc


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
        start = time.monotonic()
        try:
            for doc in self._cursor:
                yield doc
            log.info("mongo.%s iter END db=%s coll=%s",
                      self._op, self._db, self._coll)
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.%s iter FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._op, self._db, self._coll, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            try:
                self._cursor.close()
            except Exception:
                pass
            _convert_mongo_exception(exc, elapsed, f"{self._op}_iter",
                                     self._db, self._coll)
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
        start = time.monotonic()
        try:
            cursor = self._coll.aggregate(pipeline, *args, **kwargs)
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.aggregate FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "aggregate",
                                     self._db_name, self._coll_name)
            raise
        return TimedCursor(cursor, "aggregate", self._db_name, self._coll_name)

    def find(self, *args, batch_size: int | None = None, **kwargs):
        if batch_size is not None:
            kwargs["batch_size"] = batch_size
        filt = args[0] if args else kwargs.get("filter", {})
        proj = args[1] if len(args) > 1 else kwargs.get("projection")
        log.info("mongo.find START db=%s coll=%s filter=%s projection=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(proj), q(kwargs))
        start = time.monotonic()
        try:
            cursor = self._coll.find(*args, **kwargs)
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.find FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "find",
                                     self._db_name, self._coll_name)
            raise
        return TimedCursor(cursor, "find", self._db_name, self._coll_name)

    def find_one(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        proj = args[1] if len(args) > 1 else kwargs.get("projection")
        log.info("mongo.find_one START db=%s coll=%s filter=%s projection=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(proj), q(kwargs))
        start = time.monotonic()
        try:
            result = self._coll.find_one(*args, **kwargs)
            log.info("mongo.find_one END db=%s coll=%s hit=%s",
                      self._db_name, self._coll_name, result is not None)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.find_one FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "find_one",
                                     self._db_name, self._coll_name)
            raise

    def count_documents(self, *args, **kwargs):
        filt = args[0] if args else kwargs.get("filter", {})
        log.info("mongo.count_documents START db=%s coll=%s filter=%s opts=%s",
                  self._db_name, self._coll_name, q(filt), q(kwargs))
        start = time.monotonic()
        try:
            result = self._coll.count_documents(*args, **kwargs)
            log.info("mongo.count_documents END db=%s coll=%s n=%d",
                      self._db_name, self._coll_name, result)
            return result
        except Exception as exc:
            elapsed = time.monotonic() - start
            log.info("mongo.count_documents FAIL db=%s coll=%s elapsed_s=%.3f exc=%s: %s %s",
                      self._db_name, self._coll_name, elapsed,
                      type(exc).__name__, exc, _exc_detail(exc))
            _convert_mongo_exception(exc, elapsed, "count_documents",
                                     self._db_name, self._coll_name)
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

    def __init__(self, env_var_name: str = "MONGO_FRONTEND_connectionString") -> None:
        """env_var_name selects which cluster's connection string is read
        from the runtime environment. Default preserves REQ-B-002 single-
        URI behavior for existing front-end callers. Pipeline code passes
        'MONGO_connectionString' for the pipeline cluster or a per-cluster
        name like 'MONGO_CLUSTER_<x>_connectionString' for migration
        workloads. One MongoClient singleton cached per distinct env-var
        (REQ-B-003 refined for multi-cluster)."""
        global cached_client
        self._env_var_name = env_var_name

        if env_var_name in _client_cache:
            if env_var_name == "MONGO_FRONTEND_connectionString":
                cached_client = _client_cache[env_var_name]
            return

        uri = os.environ.get(env_var_name)
        if not uri:
            raise ChatHealthyException(
                mode="mongo_env_unset",
                message=(
                    f"{env_var_name} not set in the runtime environment; "
                    "ChatHealthyMongoUtilities cannot connect."
                ),
                component="ChatHealthyMongoUtilities",
            )

        kwargs = {"timeoutMS": TIMEOUT_MS}
        appname = os.environ.get("CHATHEALTHY_SERVICE_NAME")
        if appname:
            kwargs["appname"] = appname

        client = MongoClient(uri, **kwargs)
        _client_cache[env_var_name] = client
        if env_var_name == "MONGO_FRONTEND_connectionString":
            cached_client = client
        log.info(
            "MongoClient established for %s (appname=%s timeoutMS=%d)",
            env_var_name, appname or "<unset>", TIMEOUT_MS,
        )

    def getConnection(self) -> TimedClient:
        return TimedClient(_client_cache[self._env_var_name])

    def getRawClient(self) -> MongoClient:
        """Return the raw MongoClient singleton, bypassing the TimedClient
        instrumentation wrapper. ONLY for callers that must avoid the
        per-op log emissions TimedClient produces — the Mongo log handler
        in particular, where wrapped writes would recurse infinitely
        through the logging framework. All other call sites MUST use
        getConnection()."""
        return cached_client
