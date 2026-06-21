# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ChatHealthyLoggingService — canonical logger for ChatHealthy.ai code.

Realizes EPIC-003-F-005-S-001 REQs B-001..B-007 + B-009 + B-010.
Design: FrontEndApplicationLib/architectureAndDesign/
        EPIC-003-F-005-Manage-Logging-design-v9.docx
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from datetime import datetime, timezone
from typing import Optional

from .exceptions import ChatHealthyException


_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_lock = threading.Lock()
_bound_destination: Optional[str] = None
_bound_level: Optional[int] = None
_mongo_handler_bound: bool = False


# ── Mongo handler (admin.HuggingFaceLogs_{env}) ──────────────────────────
class _MongoLogHandler(logging.Handler):
    """Synchronous writer to admin.HuggingFaceLogs_{env}. Each log record
    becomes one document tagged with env + space_name. Any failure crashes
    the process via os._exit(1) (operator directive 2026-06-21)."""

    def __init__(self, conn_str: str, env: str, space_name: str) -> None:
        super().__init__()
        self._conn_str = conn_str
        self._env = env
        self._space_name = space_name
        self._coll = None  # lazy — connect on first emit
        self._lock = threading.Lock()
        # Drop pymongo's own records here only — they would recurse back
        # into the Mongo writer via the monitor thread and deadlock. The
        # file handler still receives them so DEBUG mode is preserved.
        self.addFilter(lambda r: not r.name.startswith("pymongo."))

    def _get_coll(self):
        if self._coll is None:
            with self._lock:
                if self._coll is None:
                    # Lazy import resolves the circular dependency with
                    # mongo_utilities (which imports this module for its
                    # own logger). getRawClient() returns the bare
                    # MongoClient — the TimedClient wrapper would log
                    # every insert through this very handler and recurse.
                    from .mongo_utilities import ChatHealthyMongoUtilities
                    client = ChatHealthyMongoUtilities().getRawClient()
                    self._coll = client["admin"][f"HuggingFaceLogs_{self._env}"]
        return self._coll

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._get_coll().insert_one({
                "ts": datetime.now(timezone.utc),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "env": self._env,
                "space_name": self._space_name,
            })
        except Exception as exc:
            sys.stderr.write(
                f"\n*** FATAL: ChatHealthyLoggingService write to "
                f"admin.HuggingFaceLogs_{self._env} failed: "
                f"{type(exc).__name__}: {exc}\n"
            )
            sys.stderr.flush()
            os._exit(1)


def _maybe_build_mongo_handler() -> Optional[logging.Handler]:
    """Build the Mongo handler when env supplies the needed bindings."""
    conn = os.environ.get("MONGO_FRONTEND_connectionString")
    space_name = os.environ.get("CH_SPACE_NAME", "").strip()
    env = os.environ.get("ENV_PREFIX", "").strip()
    if not (conn and space_name and env):
        return None
    return _MongoLogHandler(conn_str=conn, env=env, space_name=space_name)


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
        own_tb = super().formatException(ei)
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
        _ensure_configured()

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
