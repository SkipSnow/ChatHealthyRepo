# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The generic Gate — the one node that knows transport exists.

Every ChatHealthy front-end service (SharedServices, EvaluateCare,
FindCare) is a FastAPI process. The transport shell each of them stood up
by hand — the app object, the CORS policy, the two exception handlers that
turn a ChatHealthyException back into the response its raise site asked for
and catch an unhandled fault as a 503, and the ``/gate`` entrance that
funnels a client call to the UniversalNavigator — was copied three times
and drifted. This module is that shell, once.

The Gate carries HTTP and nothing else. It collects the session token, the
HTTP head, and the GET/POST parameters (each if present), builds the
transport-neutral request the navigator declares, hands it to
``navigator.handle_gate``, and shapes what comes back into a FastAPI
response — a stream, a bytes blob, a file download, or JSON. No op is
answered here. What an op means is the navigator's to decide, so this
module holds no opinion about any of it and a new op needs nothing from it.

Two entry points:

  * ``build_gate_app`` — construct the FastAPI app with CORS and the two
    exception handlers, parameterised by the component that owns the
    process. Every service calls this instead of standing up its own shell.

  * ``install_gate_route`` — mount the one ``/gate`` entrance on an app,
    wired to that process's navigator. Only the node that accepts external
    client traffic (SharedServices, which owns the UniversalNavigator)
    installs it; a satellite that carries no navigator does not.
"""
from __future__ import annotations

import datetime as dt
import json as _json
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib import http_request_facts as request_facts
from chathealthy_lib.authentication import AuthToken, SessionToken
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()


# ─────────────────────────────────────────────────────────────────────
# App shell — CORS and the two exception handlers, once.
# ─────────────────────────────────────────────────────────────────────
def build_gate_app(
    *,
    title: str,
    component: str,
    cors_allow_origins: list[str],
    cors_allow_origin_regex: str,
    cors_allow_credentials: bool,
    error_body_includes_mode: bool = False,
) -> FastAPI:
    """Build the FastAPI app the component runs on.

    Installs the CORS policy the component names and the two exception
    handlers every service shares: a ChatHealthyException is turned back
    into the response its raise site asked for, and any other unhandled
    fault answers 503. ``component`` names the process in the 503 body and
    in the log line. ``error_body_includes_mode`` adds the failure mode to
    the ChatHealthyException response body so a client panel can select its
    wording from where the failure happened.
    """
    app = FastAPI(title=title)

    @app.exception_handler(ChatHealthyException)
    async def _chathealthy_exception_to_response(request, exc: ChatHealthyException):
        """Return the response the raise site asked for.

        Raising ChatHealthyException instead of HTTPException moves the
        status code into the exception's context. This turns it back into
        the same response the client used to receive: same code, same
        detail body. Any other mode is an unhandled fault and answers 500.
        """
        # The boundary logs. Throwers were stripped of their log calls
        # because the rule says the catcher logs, and this is the catcher:
        # without this line a converted failure reaches the client as a
        # status code and leaves no trace anywhere of what happened.
        status = (int(exc.context.get("status_code", 500))
                  if exc.mode == "http_error" else 500)
        # exc= takes a constructed ChatHealthyException or one bound by an
        # except clause; a parameter annotated as one is neither, so the
        # facts go in the line itself rather than bending the rule to fit
        # this frame.
        log.error("%s %s -> %s  mode=%s component=%s  %s",
                  request.method, request.url.path, status,
                  exc.mode, exc.component or "-", exc.message)
        content = {"detail": exc.message}
        if error_body_includes_mode:
            # The mode travels with the response. It is what the panel that
            # has to say what was being attempted selects its wording from,
            # so the wording is decided where the failure happened and not
            # on the client.
            content["mode"] = exc.mode
        return JSONResponse(status_code=status, content=content)

    @app.exception_handler(Exception)
    async def fatal(request: Request, exc: Exception):
        # Safety net for UNHANDLED exceptions per
        # EPIC-008-F-002-S-009-REQ-B-008 Mode 3 (unhandled, not expected).
        # Reaching here is always user-fatal (503 to the user) — that IS the
        # Mode 3 definition — so tag fatal_error True. The architectural goal
        # is for Mode 3 occurrences to be RARE; each one observed in the log
        # MUST be moved to a local catch with Mode 1 or Mode 2 handling.
        log.exception("unhandled exception on %s", request.url.path,
                      extra={"fatal_error": True})
        return JSONResponse(
            status_code=503,
            content={"service": component, "source": "unhandled",
                     "time": dt.datetime.now(dt.timezone.utc).isoformat()},
        )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_allow_origins,
        allow_origin_regex=cors_allow_origin_regex,
        allow_credentials=cors_allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return app


# ─────────────────────────────────────────────────────────────────────
# /gate response instrumentation. Kept out of the gate() body so Rule-005
# (no log call in a function body that also raises ChatHealthyException)
# does not trip.
# ─────────────────────────────────────────────────────────────────────
async def _gate_instrumented_stream(inner, op_name):
    bytes_sent = 0
    lines_sent = 0
    kinds: list = []
    buf = b""

    def _ingest_line(line_bytes: bytes) -> None:
        ln = line_bytes.strip()
        if not ln:
            return
        try:
            obj = _json.loads(ln)
            kinds.append(str(obj.get("kind") or "?"))
        except Exception:
            # Mode 1 (REQ-B-008): instrumentation-only parse; on failure we
            # tag the kind as "PARSE_ERR" and the stream continues.
            # Deliberately silent (no log) — this is forensic kind-list
            # accounting, not the canonical error channel for the stream.
            kinds.append("PARSE_ERR")

    try:
        async for chunk in inner:
            if isinstance(chunk, (bytes, bytearray)):
                b = bytes(chunk)
            else:
                b = str(chunk).encode("utf-8")
            bytes_sent += len(b)
            lines_sent += b.count(b"\n")
            buf += b
            parts = buf.split(b"\n")
            buf = parts[-1]
            for ln_bytes in parts[:-1]:
                _ingest_line(ln_bytes)
            yield chunk
        if buf.strip():
            _ingest_line(buf)
        log.info(
            "/gate stream COMPLETE op=%s bytes=%d lines=%d kinds=%s",
            op_name, bytes_sent, lines_sent, kinds,
        )
    except Exception as exc:
        # Mode 2 (REQ-B-008): instrumentation catch — records the partial
        # stream state (op + bytes/lines emitted + kinds seen) for forensic
        # context, then re-raises so the actual exception continues to
        # whatever handles it upstream (a local catch, or the catch-all
        # safety net). The user-affecting outcome is owned by the upstream
        # handler; this catch only adds observability.
        log.error(
            "/gate stream BROKE op=%s bytes_emitted=%d lines_emitted=%d kinds=%s exc=%s: %s",
            op_name, bytes_sent, lines_sent, kinds,
            type(exc).__name__, exc,
        )
        raise


def _gate_log_ndjson_bytes_complete(op: str, body) -> None:
    byte_len = len(body) if isinstance(body, (bytes, bytearray)) else len(str(body).encode("utf-8"))
    line_count = body.count(b"\n") if isinstance(body, (bytes, bytearray)) else str(body).count("\n")
    log.info(
        "/gate ndjson_bytes COMPLETE op=%s bytes=%d lines=%d",
        op, byte_len, line_count,
    )


def _gate_log_json_complete(op: str) -> None:
    log.info("/gate json COMPLETE op=%s", op)


def _verify_session_or_401(origin: str, session_token_dict) -> tuple[SessionToken, str]:
    """Validate the /gate body's session_token and return (SessionToken, GUID).

    Raises ChatHealthyException(http_error, 401) on missing/invalid/
    unverified token. Lives outside the gate handler so its log-free raises
    are not co-located with the handler's own catch-all ChatHealthyException
    raise (Rule-005-B-010: the catcher logs, not the thrower).
    """
    if not isinstance(session_token_dict, dict) or not session_token_dict:
        raise ChatHealthyException(
            mode="http_error",
            component="app",
            message="session_token is required",
            status_code=401)
    try:
        st_in = SessionToken.model_validate(session_token_dict)
        at = AuthToken(st_in, origin=origin)
        valid = at.verify()
    except (ValueError, TypeError) as _e:
        raise ChatHealthyException(
            mode="http_error",
            component="app",
            message=f"session_token invalid: {_e}",
            status_code=401,
            exception=_e)
    if not valid:
        raise ChatHealthyException(
            mode="http_error",
            component="app",
            message="session_token verification failed",
            status_code=401)
    return st_in, st_in.get_auth_token()


def _log_gate_entry(op: str, intent, body_keys: list) -> None:
    """Emit the /gate entry-log line from outside the gate handler so the
    handler's own ChatHealthyException raise does not co-locate with a log
    call (Rule-005-B-010)."""
    log.debug("/gate ENTRY op=%s intent=%r body_keys=%s", op, intent, body_keys)


# ─────────────────────────────────────────────────────────────────────
# /gate — the universal entrance. Installed only on the node that owns a
# navigator and accepts external client traffic.
# ─────────────────────────────────────────────────────────────────────
def install_gate_route(app: FastAPI, *, navigator, gate_request_cls, origin: str) -> None:
    """Mount the one ``/gate`` entrance, wired to this process's navigator.

    ``navigator`` is the object whose ``handle_gate(gate_request) ->``
    response the Gate calls; its response duck-types ``.body_kind`` /
    ``.body_data``. ``gate_request_cls`` is the transport-neutral request
    the navigator declares (``op, payload, intent, session_guid,
    want_ndjson, client_ip``); the Gate fills it from the HTTP call and
    knows nothing of what the navigator does with it. ``origin`` is the
    component name the session-token signature is verified against.
    """

    async def gate(request: Request):
        """Single entrance for every client call.

        HTTP plumbing only: collect the session token, the HTTP head, and
        the GET/POST parameters (each if present), verify the token's
        signature, hand the call to the navigator, then shape the returned
        response into a FastAPI response (Streaming, bytes-NDJSON, file, or
        JSON).

        Session continuity comes from the body-level ``session_token`` field
        ClientRouter threads from its in-memory ``_sessionToken``. Every op
        verifies the token's signature; if verification passes, the session
        GUID is extracted from the verified token to hydrate the
        user_object. There is no op exempt from that — a call with no valid
        token is answered 401 whatever it names. Cookies are not used —
        HuggingFace Spaces' edge proxy strips
        Access-Control-Allow-Credentials from OPTIONS preflights.
        """
        # The GET/POST parameters, each if present: a POST carries a JSON
        # body, a GET carries a query string. Either becomes the one payload
        # the Gate reads its gesture from.
        if request.method == "POST":
            try:
                parsed = await request.json()
            except Exception:  # noqa: BLE001 - an unparsable body is simply no gesture
                parsed = None
            payload = dict(parsed or {})
        else:
            payload = dict(request.query_params)

        # A call names its gesture. A body with no op is a caller error
        # rather than a boot: session establishment is /auth/issue and
        # nothing else.
        op = str(payload.get("op") or "")
        op_payload = payload.get("payload") or {}
        intent = payload.get("intent")
        _log_gate_entry(op, intent, sorted(list(payload.keys())))

        # The HTTP head decides the wire shape and locates the client.
        accept = (request.headers.get("accept") or "").lower()
        want_ndjson = "application/x-ndjson" in accept or "text/event-stream" in accept
        # Client IP for safety-lockout hydration. X-Forwarded-For wins
        # because Cloudflare and HF proxies put the real client there; bare
        # request.client.host falls back when there's no proxy (local docker).
        xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        client_ip = xff or (request.client.host if request.client else "")

        # Every /gate call MUST carry a valid signed SessionToken. /gate is
        # the ONLY session-validation site in the system; downstream
        # services trust this verification and do not re-validate.
        st_in, session_guid = _verify_session_or_401(origin, payload.get("session_token"))

        # What this request carries, said once. Everything below reads it
        # from http_request_facts rather than being handed it, so a
        # component deep in the call tree needs no parameter to know whose
        # request it serves.
        request_facts.state_the_facts(
            token=st_in, posted=payload, headers=dict(request.headers))

        try:
            gate_req = gate_request_cls(
                op=op,
                payload=op_payload,
                intent=intent,
                session_guid=session_guid,
                want_ndjson=want_ndjson,
                client_ip=client_ip,
            )
            gate_resp = await navigator.handle_gate(gate_req)

            if gate_resp.body_kind == "ndjson_stream":
                resp: Any = StreamingResponse(
                    _gate_instrumented_stream(gate_resp.body_data, op),
                    media_type="application/x-ndjson",
                )
            elif gate_resp.body_kind == "ndjson_bytes":
                _gate_log_ndjson_bytes_complete(op, gate_resp.body_data)
                resp = Response(
                    content=gate_resp.body_data, media_type="application/x-ndjson",
                )
            elif gate_resp.body_kind == "file":
                # A download, answered through the one entrance. body_data is
                # {media_type, filename, content}.
                f = gate_resp.body_data
                resp = Response(
                    content=f["content"], media_type=f["media_type"],
                    headers={"Content-Disposition":
                             f'attachment; filename="{f["filename"]}"'},
                )
            else:  # "json"
                _gate_log_json_complete(op)
                resp = JSONResponse(content=gate_resp.body_data)

            return resp
        except ChatHealthyException:
            # The failure was named where it happened. Wrapping it here would
            # replace that name with this one, and the panel that has to say
            # what was being attempted reads the name.
            raise
        except Exception as exc:
            # REQ-T-009: any /gate exception MUST log the full stack with the
            # originating request shape. The browser sees HTTP 500 and
            # renders its hard-fail page.
            raise ChatHealthyException(
                mode="gate_failed",
                message=f"/gate failed: method={request.method} path={request.url.path} op={op} intent={intent!r}: {type(exc).__name__}: {exc}",
                component=origin,
                exception=exc,
            )

    app.add_api_route(
        "/gate",
        gate,
        methods=["POST", "GET"],
        operation_id="UniversalGate",
        openapi_extra={
            "x-implementing-class": "UniversalNavigationTool via chathealthy_lib.gate",
            "x-implementing-file": "ChatHealthyLib/src/chathealthy_lib/gate.py",
        },
    )
