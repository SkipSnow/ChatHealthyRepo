# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""EPIC-008-F-011-S-001-REQ-B-002 / REQ-B-003 — uniform fatal-error contract
for every ChatHealthy.ai backend service.

REQ-B-002: error response sent to the client must NOT reveal significant facts
about the application. The response carries only: API name, rough source of
the error, time, service. No traceback, no exception class, no stack frames,
no third-party error strings that could disclose internals.

REQ-B-003: the same fatal must produce a detailed log entry server-side
(method, path, api, source, client, cause, full traceback) so it can be
debugged later.

Usage:

    from runtime_governance import ChatHealthyFatalError, register_fatal_handler

    app = FastAPI()
    register_fatal_handler(app, service_name="findcare")

    @app.post("/something")
    def do_something():
        try:
            risky()
        except SomeError as e:
            raise ChatHealthyFatalError(api="/something", source="risky") from e
"""

from __future__ import annotations

import datetime
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class ChatHealthyFatalError(Exception):
    """Raised when a backend cannot proceed with the user's request.

    Endpoints raise this with a coarse-grained `api` (the endpoint or operation
    name) and a coarse-grained `source` (e.g. "mongo connect", "cert read").
    The global handler converts it into a 503 with a sanitized body and a
    detailed server log."""

    def __init__(self, api: str, source: str):
        self.api = api
        self.source = source
        super().__init__(f"{api}: {source}")


def register_fatal_handler(app: FastAPI, service_name: str) -> None:
    """Register the global ChatHealthyFatalError handler AND a generic Exception
    handler on the FastAPI app.

    The generic handler catches every uncaught Exception in every endpoint and
    converts it to the same 503 + sanitized body + detailed log contract as
    ChatHealthyFatalError. This means an endpoint author does NOT need to wrap
    their code in try/except to satisfy REQ-B-001/B-002/B-003 — any escape
    becomes the uniform fatal response. Endpoints can still raise
    ChatHealthyFatalError explicitly to attach a coarse `source` label;
    otherwise the generic handler labels it "unhandled".

    HTTPException is intentionally NOT caught here — FastAPI's built-in
    HTTPException handler already produces the appropriate 4xx for legitimate
    client-fault responses, which are NOT fatal."""
    log = logging.getLogger(f"{service_name}.fatal")

    @app.exception_handler(ChatHealthyFatalError)
    async def _fatal_handler(request: Request, exc: ChatHealthyFatalError):
        log.error(
            "FATAL %s %s | api=%s source=%s | client=%s | cause=%r\n%s",
            request.method,
            request.url.path,
            exc.api,
            exc.source,
            request.client.host if request.client else "unknown",
            exc.__cause__,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "api": exc.api,
                    "source": exc.source,
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "service": service_name,
                }
            },
        )

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception):
        log.error(
            "UNHANDLED %s %s | client=%s | %s: %s\n%s",
            request.method,
            request.url.path,
            request.client.host if request.client else "unknown",
            type(exc).__name__,
            exc,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "api": request.url.path,
                    "source": "unhandled",
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    "service": service_name,
                }
            },
        )
