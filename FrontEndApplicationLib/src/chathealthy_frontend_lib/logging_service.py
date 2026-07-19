# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ChatHealthyLoggingService - canonical logger for ChatHealthy.ai code.

Realizes EPIC-008-F-002-S-011 REQs B-001..B-007 + B-009 + B-010.

Mongo records conform to ChatHealthyLogsSchema.json published at
{env}.chathealthy.ai/schemas/ChatHealthyLogsSchema.json. The schema is
closed (additionalProperties: false); this handler emits only the
fields the schema declares.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Optional

from .exceptions import ChatHealthyException


# Per-request log context. ASGI middleware in each service binds the
# session_guid at request entry; every log emit on the same async task
# inherits it. Resets at request exit via the token returned from
# bind_request_context().
_log_context: ContextVar[Optional[dict]] = ContextVar("ch_log_context", default=None)


def bind_request_context(*, session_guid: Optional[str] = None) -> Any:
    """Bind per-request log context. Returns a token to pass to
    clear_request_context() at request end."""
    return _log_context.set({"session_guid": session_guid})


def clear_request_context(token: Any) -> None:
    """Reset to the prior context (called from the request finally block)."""
    _log_context.reset(token)


def bind_user_object_to_log(user_object: Any) -> None:
    """Bind the user_object's GUID to the log context. After this call,
    every log emitted on the same async task carries session_guid +
    user_action=true. Framework call — handle_gate invokes this once
    the user_object exists. Developers using this library NEVER need
    to extract the GUID themselves; user_object is the source of truth.

    Idempotent — re-binding with the same async task overwrites the
    prior value. Per-request isolation is automatic because each
    asgi request runs in its own async task and contextvars are
    per-task.

    Per EPIC-002-F-003-S-003-REQ-B-001 the GUID lives at
    user_object.current_session_token.get_auth_token() (first 32 bytes
    of the assembled session token).
    """
    guid: Optional[str] = None
    try:
        ct = getattr(user_object, "current_session_token", None)
        if ct is not None and hasattr(ct, "get_auth_token"):
            g = ct.get_auth_token()
            if isinstance(g, str) and len(g) == 32:
                guid = g
    except Exception:
        guid = None
    _log_context.set({"session_guid": guid})


_FORMAT = "%(asctime)s %(levelname)s %(name)s - %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()
_bound_destination: Optional[str] = None
_bound_level: Optional[int] = None
_mongo_handler_bound: bool = False


class _Formatter(logging.Formatter):
    """Overrides Formatter.formatException to render ChatHealthyException
    with every attribute and both stack traces (REQ-B-007)."""

    def formatException(self, ei):  # type: ignore[override]
        _, exc_value, _ = ei
        if not isinstance(exc_value, ChatHealthyException):
            return super().formatException(ei)
        attrs = [
            f"mode={exc_value.mode!r}",
            f"message={exc_value.message!r}",
        ]
        if exc_value.component is not None:
            attrs.append(f"component={exc_value.component!r}")
        if exc_value.server is not None:
            attrs.append(f"server={exc_value.server!r}")
        for k, v in (exc_value.context or {}).items():
            attrs.append(f"context.{k}={v!r}")
        head = "ChatHealthyException(" + ", ".join(attrs) + ")"
        # Destination (ChatHealthyException) traceback: when the exception
        # was constructed-only (log.error(exc=ChatHealthyException(...))
        # with no `raise`), Python attached no __traceback__, so
        # formatException returns just "NoneType: None". Fall back to the
        # stack we captured at construction time so the destination always
        # has a real traceback in the formatted log document.
        if exc_value.__traceback__ is not None:
            own_tb = super().formatException(ei)
        else:
            construction_stack = getattr(exc_value, "construction_stack", "") or ""
            own_tb = (
                "Traceback (most recent call last):\n"
                + construction_stack
                + type(exc_value).__name__ + ": " + str(exc_value)
            )
        wrapped = exc_value.exception
        if wrapped is None:
            return head + "\n" + own_tb
        wrapped_ei = (type(wrapped), wrapped, wrapped.__traceback__)
        wrapped_tb = super().formatException(wrapped_ei)
        return (
            head + "\n"
            + "--- ChatHealthyException traceback ---\n" + own_tb + "\n"
            + "--- wrapped " + type(wrapped).__name__ + " traceback ---\n" + wrapped_tb
        )


class _MongoLogHandler(logging.Handler):
    """Synchronous writer to admin.ChatHealthyLogs_{env}. Each emit
    produces one document conforming to ChatHealthyLogsSchema.json. Any
    insert failure crashes the process via os._exit(1) per operator
    directive (no silent loss)."""

    def __init__(self, env: str, target: str) -> None:
        super().__init__()
        self._env = env
        self._target = target
        self._coll = None  # lazy - connect on first emit
        self._lock = threading.Lock()
        # Re-entrancy guard: any log records produced by code we call
        # while emitting (mongo_utilities __init__'s 'MongoClient
        # established' line, pymongo monitor thread) get DROPPED to
        # break the deadlock cycle. The file handler still gets them.
        self._in_emit = threading.local()
        # Pymongo's own records would recurse back through here via
        # the monitor thread - drop them at handler entry.
        self.addFilter(lambda r: not r.name.startswith("pymongo."))

    def _get_coll(self):
        if self._coll is None:
            with self._lock:
                if self._coll is None:
                    # Lazy import resolves the circular with
                    # mongo_utilities (which imports this module). The
                    # raw client bypasses TimedClient because TimedClient
                    # logs every op, which would recurse here.
                    from .mongo_utilities import ChatHealthyMongoUtilities
                    client = ChatHealthyMongoUtilities().getRawClient()
                    self._coll = client["admin"][f"ChatHealthyLogs_{self._env}"]
        return self._coll

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._in_emit, "value", False):
            return
        self._in_emit.value = True
        try:
            ctx = _log_context.get(None) or {}
            ctx_guid = ctx.get("session_guid")
            doc: dict = {
                "timeStamp": datetime.now(timezone.utc),
                "level": record.levelname,
                "env": self._env,
                "target": self._target,
                "formatted": self.format(record),
                "pathname": record.pathname,
                "fatal_error": False,
                "user_action": bool(ctx_guid),
                # session_guid is ALWAYS present on every doc. Null when
                # there is no user session in flight (system / startup /
                # background); the GUID string when there is.
                "session_guid": ctx_guid,
            }
            # Optional source-location fields per schema.
            if record.lineno is not None:
                doc["lineno"] = record.lineno
            if record.funcName:
                doc["funcName"] = record.funcName
            # Exception type as a structured query field. The full
            # tracebacks live inside `formatted` because the _Formatter
            # renders them.
            if record.exc_info:
                exc_type = record.exc_info[0]
                if exc_type is not None:
                    doc["exc_type"] = exc_type.__name__
            # Schema-defined extras: callers pass extra={...} on log()
            # to override these defaults. Each is a direct check — no
            # abstraction, no policy.
            if hasattr(record, "fatal_error"):
                doc["fatal_error"] = record.fatal_error
            if hasattr(record, "user_action"):
                doc["user_action"] = record.user_action
            if hasattr(record, "session_guid"):
                doc["session_guid"] = record.session_guid
            if hasattr(record, "http_status"):
                doc["http_status"] = record.http_status
            if hasattr(record, "http_path"):
                doc["http_path"] = record.http_path
            self._get_coll().insert_one(doc)
        except Exception as exc:
            sys.stderr.write(
                f"\n*** FATAL: ChatHealthyLoggingService write to "
                f"admin.ChatHealthyLogs_{self._env} failed: "
                f"{type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()
            os._exit(1)
        finally:
            self._in_emit.value = False


def _maybe_build_mongo_handler() -> logging.Handler:
    """Build the Mongo handler. All three env bindings are required
    (per operator directive 2026-07-19: "if you can't log to Mongo you
    die"). Silent-skip on missing env was an anti-pattern that let
    services boot with a broken observability surface; this now raises
    ChatHealthyException so the caller aborts."""
    target = os.environ.get("CH_SPACE_NAME", "").strip()
    env = os.environ.get("ENV_PREFIX", "").strip()
    conn = os.environ.get("MONGO_FRONTEND_connectionString")
    missing = []
    if not conn:
        missing.append("MONGO_FRONTEND_connectionString")
    if not target:
        missing.append("CH_SPACE_NAME")
    if not env:
        missing.append("ENV_PREFIX")
    if missing:
        raise ChatHealthyException(
            mode="mongo_log_handler_env_unset",
            message=(
                "ChatHealthyLoggingService cannot wire the Mongo handler "
                "because required env binding(s) are missing: "
                f"{', '.join(missing)}. Operating without a durable "
                "observability surface is a fatal configuration."
            ),
            component="ChatHealthyLoggingService",
            missing=",".join(missing),
        )
    h = _MongoLogHandler(env=env, target=target)
    # Share the file handler's _Formatter so `formatted` is byte-equivalent
    # to the file log line, including ChatHealthyException stacks.
    h.setFormatter(_Formatter(fmt=_FORMAT, datefmt=_DATEFMT))
    return h


def _compute_destination() -> str:
    dest = os.environ.get("CH_LOG_DESTINATION", "./logs")
    if dest in ("stdout", "stderr"):
        return dest
    comp = os.environ.get("CH_COMPONENT", "chathealthy")
    return f"{dest.rstrip('/')}/{comp}.log"


def _compute_level() -> int:
    name = os.environ.get("CH_LOG_LEVEL", "INFO").upper()
    mapped = logging.getLevelName(name)
    return mapped if isinstance(mapped, int) else logging.INFO


def _build_handler(destination: str) -> logging.Handler:
    if destination == "stdout":
        h: logging.Handler = logging.StreamHandler(stream=sys.stdout)
    elif destination == "stderr":
        h = logging.StreamHandler()
    else:
        parent = os.path.dirname(destination)
        if parent:
            os.makedirs(parent, exist_ok=True)
        h = logging.FileHandler(
            destination, mode="a", encoding="utf-8", errors="replace",
        )
    h.setFormatter(_Formatter(fmt=_FORMAT, datefmt=_DATEFMT))
    return h


def _ensure_configured() -> None:
    """Re-read env vars; rebind if anything changed (REQ-B-003 + REQ-B-006)."""
    global _bound_destination, _bound_level, _mongo_handler_bound
    dest = _compute_destination()
    level = _compute_level()
    if dest == _bound_destination and level == _bound_level and _mongo_handler_bound:
        return
    with _lock:
        if dest == _bound_destination and level == _bound_level and _mongo_handler_bound:
            return
        handlers: list[logging.Handler] = [_build_handler(dest)]
        mongo = _maybe_build_mongo_handler()
        if mongo is not None:
            handlers.append(mongo)
            _mongo_handler_bound = True
        logging.basicConfig(level=level, handlers=handlers, force=True)
        _bound_destination = dest
        _bound_level = level


class ChatHealthyLoggingService:
    """Canonical logger. Constructor takes zero arguments.

    See FrontEndApplicationLib/architectureAndDesign/
        EPIC-003-F-005-Manage-Logging-design-v9.docx.
    """

    def __init__(self) -> None:
        pass

    @staticmethod
    def _debug_mode_on() -> bool:
        return logging.getLogger().isEnabledFor(logging.DEBUG)

    def _emit(
        self,
        level: int,
        msg: str,
        args: tuple,
        exc: Optional[ChatHealthyException],
        if_not_debug_log: bool,
        kw: dict,
    ) -> None:
        _ensure_configured()
        if not if_not_debug_log:
            if not self._debug_mode_on():
                return
        if exc is not None:
            if not isinstance(exc, ChatHealthyException):
                raise ChatHealthyException(
                    mode="logger_exception_not_chathealthy",
                    message=(
                        "The exc argument passed to ChatHealthyLoggingService "
                        "log methods MUST be a ChatHealthyException; got "
                        f"{type(exc).__name__}."
                    ),
                    component="ChatHealthyLoggingService",
                )
            kw["exc_info"] = (type(exc), exc, exc.__traceback__)
        kw.setdefault("stacklevel", 3)
        logging.getLogger().log(level, msg, *args, **kw)

    def debug(
        self, msg: str, *args,
        exc: Optional[ChatHealthyException] = None,
        if_not_debug_log: bool = False, **kw,
    ) -> None:
        self._emit(logging.DEBUG, msg, args, exc, if_not_debug_log, kw)

    def info(
        self, msg: str, *args,
        exc: Optional[ChatHealthyException] = None,
        if_not_debug_log: bool = False, **kw,
    ) -> None:
        self._emit(logging.INFO, msg, args, exc, if_not_debug_log, kw)

    def warning(
        self, msg: str, *args,
        exc: Optional[ChatHealthyException] = None,
        if_not_debug_log: bool = False, **kw,
    ) -> None:
        self._emit(logging.WARNING, msg, args, exc, if_not_debug_log, kw)

    def error(
        self, msg: str, *args,
        exc: Optional[ChatHealthyException] = None,
        if_not_debug_log: bool = False, **kw,
    ) -> None:
        self._emit(logging.ERROR, msg, args, exc, if_not_debug_log, kw)

    def critical(
        self, msg: str, *args,
        exc: Optional[ChatHealthyException] = None,
        if_not_debug_log: bool = False, **kw,
    ) -> None:
        self._emit(logging.CRITICAL, msg, args, exc, if_not_debug_log, kw)

    def exception(
        self, msg: str, *args,
        exc: Optional[ChatHealthyException] = None,
        if_not_debug_log: bool = False, **kw,
    ) -> None:
        if exc is None:
            kw.setdefault("exc_info", True)
        self._emit(logging.ERROR, msg, args, exc, if_not_debug_log, kw)
