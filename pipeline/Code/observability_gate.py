# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Observability gate for pipeline component boot.

Pure library shared by pipeline components (Control, Worker, runbook).
Every pipeline component MUST run this gate as the first thing it does
after wiring stdlib logging. Per operator requirements 2026-07-18:

  1. Try to connect to Mongo via ChatHealthyMongoUtilities. If we can't,
     throw a ChatHealthyException; the exception library catches and
     dumps as much information as possible to stderr. The original
     exception is attached to the ChatHealthyException via the
     `exception` kwarg.
  2. If we DID connect, try to write one log message per criticality
     level (debug, info, warning, error, critical). Message body:
     'Hello World ChatHealthyLogTest{level}'. Each emission has its
     own try/catch/throw with a robust message and the original
     exception attached.

Exception discipline: this lib writes diagnostic detail to stderr and
raises ChatHealthyException on failure. It does NOT terminate the
process -- the caller decides the die policy.
"""
from __future__ import annotations

import sys
import traceback

from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


class ObservabilityGate:
    """Prove Mongo is reachable AND that every log level emits, before
    doing any real work. Constructed per boot; call check()."""

    _LEVELS = ("debug", "info", "warning", "error", "critical")

    def __init__(self, *, component: str, server: str) -> None:
        self._component = component
        self._server = server

    def check(self) -> None:
        """Step 1: connect to Mongo. Step 2: emit one log per criticality
        level. Returns None on success. Raises ChatHealthyException with
        the original chained on any failure. Diagnostic detail is
        dumped to stderr immediately before raising."""
        self._verify_mongo_reachable()
        self._verify_log_probe()

    def _verify_mongo_reachable(self) -> None:
        # Try/catch: if the Mongo call throws, we catch, write stderr,
        # then throw ChatHealthyException with the original chained.
        try:
            client = ChatHealthyMongoUtilities().getConnection()
            ping_result = client.admin.command("ping")
        except ChatHealthyException as ch_exc:
            self._dump_to_stderr(ch_exc)
            raise
        except Exception as exc:
            ch_exc = ChatHealthyException(
                mode="mongo_connect_failed",
                message=(
                    "Pipeline observability gate could not establish a front-"
                    "cluster Mongo connection through ChatHealthyMongoUtilities."
                    f" Original exception type: {type(exc).__name__}. Args:"
                    f" {exc.args!r}."
                ),
                component=self._component,
                server=self._server,
                step="mongo_reachable",
                exception=exc,
            )
            self._dump_to_stderr(ch_exc)
            raise ch_exc from exc

        # ping does not throw on every failure mode -- it returns a doc
        # whose `ok` field indicates the server's verdict. If ok is not
        # exactly 1.0 the ping was not confirmed and we abend the gate:
        # write to stderr, throw ChatHealthyException.
        if not isinstance(ping_result, dict) or ping_result.get("ok") != 1.0:
            ch_exc = ChatHealthyException(
                mode="mongo_ping_not_ok",
                message=(
                    "Front-cluster Mongo ping did not confirm ok=1.0. "
                    "The connection object was returned but the server "
                    "did not acknowledge a live primary. Response: "
                    f"{ping_result!r}."
                ),
                component=self._component,
                server=self._server,
                step="mongo_reachable",
                ping_result=repr(ping_result),
            )
            self._dump_to_stderr(ch_exc)
            raise ch_exc

    def _verify_log_probe(self) -> None:
        logger = ChatHealthyLoggingService()
        for level_name in self._LEVELS:
            message = f"Hello World ChatHealthyLogTest{level_name}"
            method = getattr(logger, level_name, None)
            if method is None:
                ch_exc = ChatHealthyException(
                    mode="logging_service_method_missing",
                    message=(
                        f"ChatHealthyLoggingService has no {level_name!r} "
                        "method. The frontend-lib may be a stale build."
                    ),
                    component=self._component,
                    server=self._server,
                    step="log_probe",
                    missing_method=level_name,
                )
                self._dump_to_stderr(ch_exc)
                raise ch_exc
            try:
                method(message, if_not_debug_log=True)
            except ChatHealthyException as ch_exc:
                self._dump_to_stderr(ch_exc)
                raise
            except Exception as exc:
                ch_exc = ChatHealthyException(
                    mode="log_emit_failed",
                    message=(
                        f"ChatHealthyLoggingService.{level_name}() raised "
                        "a non-ChatHealthyException while emitting the probe "
                        f"log {message!r}. Original exception type: "
                        f"{type(exc).__name__}. Args: {exc.args!r}."
                    ),
                    component=self._component,
                    server=self._server,
                    step="log_probe",
                    level_name=level_name,
                    message_body=message,
                    exception=exc,
                )
                self._dump_to_stderr(ch_exc)
                raise ch_exc from exc

    @staticmethod
    def _dump_to_stderr(exc: ChatHealthyException) -> None:
        """Write every field the ChatHealthyException carries + the
        wrapped original + tracebacks to stderr."""
        print("*" * 78, file=sys.stderr, flush=True)
        print("ObservabilityGate: failure detail", file=sys.stderr, flush=True)
        print(f"  mode:      {exc.mode!r}", file=sys.stderr, flush=True)
        print(f"  message:   {exc.message}", file=sys.stderr, flush=True)
        print(f"  server:    {exc.server!r}", file=sys.stderr, flush=True)
        print(f"  component: {exc.component!r}", file=sys.stderr, flush=True)
        for k, v in (exc.context or {}).items():
            print(f"  ctx.{k}: {v!r}", file=sys.stderr, flush=True)
        if exc.exception is not None:
            original = exc.exception
            print("  original (chained) exception:",
                  file=sys.stderr, flush=True)
            print(f"    type: {type(original).__name__}",
                  file=sys.stderr, flush=True)
            print(f"    args: {original.args!r}",
                  file=sys.stderr, flush=True)
            print(f"    repr: {original!r}",
                  file=sys.stderr, flush=True)
            if original.__traceback__ is not None:
                print("    original traceback:",
                      file=sys.stderr, flush=True)
                traceback.print_tb(original.__traceback__, file=sys.stderr)
        print("  construction_stack:", file=sys.stderr, flush=True)
        print(exc.construction_stack, file=sys.stderr, flush=True)
        print("  live traceback (may be pre-raise):",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        print("*" * 78, file=sys.stderr, flush=True)
