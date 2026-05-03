# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""EPIC-008-F-011-S-001-REQ-B-001/B-002/B-003 — uniform fatal-error contract.

Sanitized client response (REQ-B-002): JSON body with three fields only —
`service` (friendly name: FindCare / EvaluateCare / SharedServices), `source`
(rough-grained subsystem label), `time` (ISO timestamp). The URL path is
NOT in the response (path reveals an application fact).

Detailed server log (REQ-B-003): EVERY error logs in excruciating detail —
full URL, query string, sanitized headers, request body (truncated), client
IP, session token (if present in body or Authorization header), exception
class + message, and full traceback. The body is captured by middleware so
the exception handler can read it without racing with the request handler.

Usage:

    from runtime_governance import (
        ChatHealthyFatalError,
        register_fatal_handler,
    )

    app = FastAPI()
    register_fatal_handler(app, service_name="FindCare")

    @app.post("/something")
    def do_something():
        try:
            risky()
        except SomeError as e:
            raise ChatHealthyFatalError(api="/something", source="risky") from e
"""

from __future__ import annotations

import datetime
import json
import logging
import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


_SENSITIVE_HEADERS = {"authorization", "cookie", "x-api-key", "x-auth-token"}
_BODY_LOG_LIMIT = 4000  # bytes; truncate longer bodies to keep logs sane.


class ChatHealthyFatalError(Exception):
    """Raised when a backend cannot proceed with the user's request.

    Endpoints raise this with a coarse-grained `api` (the endpoint or operation
    label, e.g. '/chat (term extraction)') and a coarse-grained `source` label
    (e.g. 'mongo connect', 'openai chat.completions'). The global handler
    converts it into a sanitized 503 response and a detailed server log."""

    def __init__(self, api: str, source: str):
        self.api = api
        self.source = source
        super().__init__(f"{api}: {source}")


def _extract_session_token(request: Request, body_bytes: bytes) -> str | None:
    """Best-effort token extraction. Looks at body JSON `session_token` first
    (the project's canonical carrier), then Authorization header. Returns a
    short string suitable for logging; never the raw long signature."""
    try:
        if body_bytes:
            parsed = json.loads(body_bytes.decode("utf-8", errors="replace"))
            if isinstance(parsed, dict):
                tok = parsed.get("session_token")
                if isinstance(tok, dict):
                    raw = tok.get("token") or ""
                    if raw:
                        return raw if len(raw) <= 80 else raw[:80] + "...(truncated)"
                elif isinstance(tok, str) and tok:
                    return tok if len(tok) <= 80 else tok[:80] + "...(truncated)"
    except Exception:
        pass
    auth = request.headers.get("authorization")
    if auth:
        return auth if len(auth) <= 80 else auth[:80] + "...(truncated)"
    return None


def _request_dump(request: Request, body_bytes: bytes) -> dict:
    """Build the dict that gets logged for every fatal — full API call detail."""
    headers = {
        k: ("<redacted>" if k.lower() in _SENSITIVE_HEADERS else v)
        for k, v in request.headers.items()
    }
    if body_bytes is None:
        body_str: str | None = None
    elif len(body_bytes) > _BODY_LOG_LIMIT:
        body_str = body_bytes[:_BODY_LOG_LIMIT].decode("utf-8", errors="replace") + f"...(truncated, {len(body_bytes)} bytes total)"
    else:
        body_str = body_bytes.decode("utf-8", errors="replace")
    return {
        "method": request.method,
        "url": str(request.url),
        "path": request.url.path,
        "query": dict(request.query_params),
        "client": request.client.host if request.client else "unknown",
        "headers": headers,
        "body": body_str,
        "session_token": _extract_session_token(request, body_bytes or b""),
    }


def register_fatal_handler(app: FastAPI, service_name: str) -> None:
    """Register the uniform fatal-error contract on this FastAPI app.

    `service_name` MUST be the user-friendly form (`FindCare`, `EvaluateCare`,
    `SharedServices`). The same string is shown to end users in the overlay
    and used as the logger name suffix (lowercased)."""
    log = logging.getLogger(f"{service_name.lower()}.fatal")

    # Body-capture middleware — Starlette consumes the body once. We re-prime
    # the receive channel so the actual handler still sees the body, AND we
    # stash it on request.state so the exception handlers can log it.
    @app.middleware("http")
    async def _capture_body_for_fatal_log(request: Request, call_next):
        body = await request.body()

        async def _replay_receive():
            return {"type": "http.request", "body": body, "more_body": False}

        request._receive = _replay_receive  # noqa: SLF001 — Starlette internal, accepted pattern
        request.state.body_bytes = body
        return await call_next(request)

    @app.exception_handler(ChatHealthyFatalError)
    async def _fatal_handler(request: Request, exc: ChatHealthyFatalError):
        body_bytes = getattr(request.state, "body_bytes", b"") or b""
        details = _request_dump(request, body_bytes)
        log.error(
            "FATAL api=%s source=%s | %s | cause=%r\n%s",
            exc.api,
            exc.source,
            json.dumps(details, default=str, ensure_ascii=False),
            exc.__cause__,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "message": f"{service_name} service is not available.",
                    "service": service_name,
                    "source": exc.source,
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
            },
        )

    @app.exception_handler(Exception)
    async def _generic_handler(request: Request, exc: Exception):
        body_bytes = getattr(request.state, "body_bytes", b"") or b""
        details = _request_dump(request, body_bytes)
        log.error(
            "UNHANDLED %s: %s | %s\n%s",
            type(exc).__name__,
            exc,
            json.dumps(details, default=str, ensure_ascii=False),
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "message": f"{service_name} service is not available.",
                    "service": service_name,
                    "source": "unhandled",
                    "time": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                }
            },
        )
