# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Observability gate for pipeline component boot.

Pure library shared by pipeline components (Control, Worker, runbook).
Every pipeline component MUST run this gate as the first thing bootstrap
does. Per operator requirements 2026-07-18:

  1. Try to connect to Mongo via ChatHealthyMongoUtilities. If we can't,
     throw ChatHealthyException; the exception library dumps to stderr.
     Original exception attached via the `exception` kwarg.
  2. Try to connect to the pipeline blob Storage account (all our IT
     data and business data resides there). Same try/catch/throw/dump
     discipline as Mongo.
  3. If both connections succeed, try to write one log message per
     criticality level (debug, info, warning, error, critical) via
     ChatHealthyLoggingService. Message body:
     'Hello World ChatHealthyLogTest{level}'. Same discipline again.

Exception discipline: this lib writes diagnostic detail to stderr and
raises ChatHealthyException on failure. It does NOT terminate the
process -- the caller decides the die policy.
"""
from __future__ import annotations

import os
import sys
import traceback

from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


_STORAGE_ACCOUNT_URL_ENV = "PIPELINE_LOG_ACCOUNT_URL"


class ObservabilityGate:
    """Prove Mongo AND blob Storage are reachable AND every log level
    emits, before doing any real work. Constructed per boot; call check()."""

    _LEVELS = ("debug", "info", "warning", "error", "critical")

    def __init__(self, *, component: str, server: str) -> None:
        self._component = component
        self._server = server

    def check(self) -> None:
        """Step 1: connect to Mongo. Step 2: connect to blob Storage.
        Step 3: emit one log per criticality level. Returns None on
        success. Raises ChatHealthyException with the original chained
        on any failure. Diagnostic detail is dumped to stderr immediately
        before raising."""
        self._verify_mongo_reachable()
        self._verify_storage_reachable()
        self._verify_log_probe()

    def _verify_mongo_reachable(self) -> None:
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

    def _verify_storage_reachable(self) -> None:
        """Prove we can reach the pipeline blob Storage account (where
        all IT + business data resides). Uses the container-run managed
        identity via DefaultAzureCredential. Probes via a data-plane
        container list (Storage Blob Data Contributor / Reader), NOT
        via get_account_information (which requires Storage Account
        Contributor / Reader and Data Access — a different role).
        Account URL comes from PIPELINE_LOG_ACCOUNT_URL; no fallback
        (deploy chain sets it from manifest)."""
        account_url = os.environ.get(_STORAGE_ACCOUNT_URL_ENV, "").strip()
        if not account_url:
            ch_exc = ChatHealthyException(
                mode="storage_account_url_env_unset",
                message=(
                    f"{_STORAGE_ACCOUNT_URL_ENV} not set in the runtime "
                    "environment; observability gate cannot probe Storage "
                    "connectivity. Deploy chain is responsible for setting "
                    "this on the container spec."
                ),
                component=self._component,
                server=self._server,
                step="storage_reachable",
            )
            self._dump_to_stderr(ch_exc)
            raise ch_exc
        try:
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobServiceClient
            client = BlobServiceClient(
                account_url=account_url,
                credential=DefaultAzureCredential(),
            )
            list(client.list_containers(results_per_page=1).by_page().__next__())
            info = {"probe": "list_containers ok"}
        except ChatHealthyException as ch_exc:
            self._dump_to_stderr(ch_exc)
            raise
        except Exception as exc:
            ch_exc = ChatHealthyException(
                mode="storage_connect_failed",
                message=(
                    "Pipeline observability gate could not reach the "
                    f"pipeline blob Storage account at {account_url!r}. "
                    "This is where all IT and business data resides; the "
                    "component cannot proceed. Original exception type: "
                    f"{type(exc).__name__}. Args: {exc.args!r}."
                ),
                component=self._component,
                server=self._server,
                step="storage_reachable",
                account_url=account_url,
                exception=exc,
            )
            self._dump_to_stderr(ch_exc)
            raise ch_exc from exc

        if not isinstance(info, dict):
            ch_exc = ChatHealthyException(
                mode="storage_account_info_unexpected",
                message=(
                    "BlobServiceClient.get_account_information returned "
                    f"a non-dict value: {info!r}. The connection object "
                    "was returned but the service did not confirm account "
                    "identity."
                ),
                component=self._component,
                server=self._server,
                step="storage_reachable",
                account_url=account_url,
                info=repr(info),
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
        ChatHealthyLoggingService().info("*" * 78)
        ChatHealthyLoggingService().info("ObservabilityGate: failure detail")
        ChatHealthyLoggingService().info(f"  mode:      {exc.mode!r}")
        ChatHealthyLoggingService().info(f"  message:   {exc.message}")
        ChatHealthyLoggingService().info(f"  server:    {exc.server!r}")
        ChatHealthyLoggingService().info(f"  component: {exc.component!r}")
        for k, v in (exc.context or {}).items():
            ChatHealthyLoggingService().info(f"  ctx.{k}: {v!r}")
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
        ChatHealthyLoggingService().info("  construction_stack:")
        ChatHealthyLoggingService().info(exc.construction_stack)
        print("  live traceback (may be pre-raise):",
              file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)
        ChatHealthyLoggingService().info("*" * 78)
