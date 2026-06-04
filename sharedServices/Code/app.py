# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SharedServices — FastAPI app on port 8002.
#
# Owner: EPIC-002-F-003 (Authorizations and Authentications).
#
# The /gate route is the universal entrance for the application. Every
# request flows through TWO PydanticAI-shaped tools in sequence:
#
#   1. authorizations_and_authentications_tool.run(AuthnDeps)
#         -> user_object (mint-or-restore + persist admin.sessions)
#   2. universal_navigation_tool.run(AgentDeps, NavRequest(op, payload))
#         -> dispatches to the graph node for `op` (boot, splash,
#            record_ux_event, utterance, ...). Emits stream events as it
#            runs; final result is the last NDJSON line on the wire.
#
# Auxiliary endpoints (/session, /verify-token, /auth/issue, /auth/google/*,
# /transfer/to-findcare, /secrets/{key}, /health) are out of scope of this
# refactor and stay direct.

import base64
import json as _json
import logging
import math
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import Body, Cookie, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("shared_services")


def _bootstrap_certs_from_env():
    runtime_dir = os.path.join(tempfile.gettempdir(), "ch_certs")
    mapping = {
        "FINDCARE_CERT_PEM":      "findcare.crt",
        "SHARED_CERT_PEM":        "shared.crt",
        "SHARED_SIGNING_KEY_PEM": "shared.key",
        "CA_CERT_PEM":            "ca.crt",
    }
    wrote = []
    for env_var, filename in mapping.items():
        b64 = os.environ.get(env_var)
        if not b64:
            continue
        try:
            pem = base64.b64decode(b64.strip())
        except Exception as e:
            _log.error("STARTUP: %s not valid base64: %s", env_var, e)
            raise
        os.makedirs(runtime_dir, exist_ok=True)
        path = os.path.join(runtime_dir, filename)
        with open(path, "wb") as f:
            f.write(pem)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        wrote.append(filename)
    if wrote:
        os.environ["CERTS_DIR"] = runtime_dir
        _log.info("startup bootstrap: wrote %s to %s", ",".join(wrote), runtime_dir)


_bootstrap_certs_from_env()

app = FastAPI(title="ChatHealthy.ai Shared Services", version="0.1.5")

from chathealthy_frontend_lib.runtime_governance import register_fatal_handler
register_fatal_handler(app, service_name="SharedServices")


class _AsgiLogMiddleware:
    """Pure-ASGI request logger. Does NOT buffer the response body, so
    StreamingResponse works through this middleware (the BaseHTTPMiddleware
    pattern used by @app.middleware('http') buffers and breaks streaming)."""

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.asgi_app(scope, receive, send)
            return
        start = time.time()
        status_holder = {"code": 0}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status_holder["code"] = message["status"]
            await send(message)

        try:
            await self.asgi_app(scope, receive, send_wrapper)
        finally:
            elapsed = round((time.time() - start) * 1000)
            client = (scope.get("client") or (None, None))[0] or "unknown"
            headers = dict(scope.get("headers") or [])
            xff = headers.get(b"x-forwarded-for", b"").decode("latin1") or client
            _log.info(
                "%s %s → %d (%dms) from %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                status_holder["code"], elapsed, xff,
            )


app.add_middleware(_AsgiLogMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://localhost", "https://localhost:443", "https://localhost:3000",
                   "https://localhost:8080", "https://localhost:8081",
                   "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ──────────────────────────────────────────────────────────

from healthcheck.health_endpoint import HealthEndpoint
from displayChrome.transfer_to_findcare_endpoint import TransferToFindCareEndpoint
from secretsManager.secrets_endpoint import SecretsEndpoint
from chathealthy_frontend_lib.authentication import (
    AuthToken, SessionRestampRequest, SessionToken, VerifyTokenResponse,
)
from authentication.mintable_auth_token import MintableAuthToken
from authentication.google_oauth_endpoint import GoogleOAuthEndpoint

# New architecture: two tools chained inside /gate.
from authentication import (
    authorizations_and_authentications_tool as authn,
    universal_navigation_tool as nav,
)
from authentication.agent_deps import AgentDeps, AuthnDeps
AUTHN_TOOL = authn.TOOL
UNIVERSAL_NAV_TOOL = nav.TOOL


_ORIGIN = "SharedServices"
_ENV = os.getenv("ENV_PREFIX", "dev")
_SESSION_COOKIE_NAME = "ch_session"
_SESSION_COOKIE_MAX_AGE = 300
_SESSION_COOKIE_DOMAIN = "chathealthy.ai"
_SESSION_COOKIE_PATH = "/gate"


def _impl(cls_name, file_subpath):
    return {
        "x-implementing-class": cls_name,
        "x-implementing-file": f"sharedServices/Code/{file_subpath}",
    }


@app.post("/health", operation_id="HealthEndpoint",
          openapi_extra=_impl("HealthEndpoint", "healthcheck/health_endpoint.py"))
def health():
    return HealthEndpoint()()


# ─────────────────────────────────────────────────────────────────────
# /gate — the universal entrance. Streams NDJSON.
# ─────────────────────────────────────────────────────────────────────

def _assemble_session_token_value(user_object) -> str:
    """Assemble the 67-byte ch_session cookie value per
    EPIC-002-F-003-S-003-REQ-B-007: GUID(32) + first_stamp(17) + 'X' + second_stamp(17).

    Inputs come from user_object.current_session_token; the GUID is the
    auth_token tail of the signed token; the nonce field carries the two
    17-byte timestamps separated by an 'X'.
    """
    st = user_object.current_session_token
    guid = st.get_auth_token()              # 32 bytes
    nonce_field = st.get_nonce()            # 17 + 1 + 17 = 35 bytes ("YYYYMMDDhhmmssfffXYYYYMMDDhhmmssfff")
    return f"{guid}{nonce_field}"           # 32 + 35 = 67 bytes


def _set_session_cookie(response: Response, user_object) -> None:
    """REQ-T-001 + REQ-T-002.

    Cookie attributes: Secure, HttpOnly, Max-Age=300, SameSite=None,
    Domain=chathealthy.ai, Path=/gate. Value is the 67-byte session-token
    assembly per REQ-B-007.

    The Domain attribute is included on every response regardless of host
    so the header satisfies REQ-T-001 byte-for-byte. Browsers that reach
    the endpoint via a host that does NOT match chathealthy.ai (localhost
    during smoke runs; the bare HF Space hostname) will silently drop the
    cookie. That is expected — production traffic reaches /gate at
    *.chathealthy.ai by way of the Pages Function fronting the HF Space,
    where the Domain attribute lines up with the browser-visible host.
    """
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=_assemble_session_token_value(user_object),
        max_age=_SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="none",
        domain=_SESSION_COOKIE_DOMAIN,
        path=_SESSION_COOKIE_PATH,
    )


def _do_history_push(mongo_client, guid: str, directive) -> None:
    if directive is None:
        return
    coll = mongo_client["Users"]["sessions"]
    coll.update_one(
        {"_id": guid},
        {"$push": {f"session_conversation_history.{directive.array}": directive.entry}},
    )


def _time_remaining_seconds(user_object) -> int:
    """REQ-T-004: floor((most_recent_restamp + 300s) - server_response_instant)
    against UTC milliseconds; clipped to non-negative integers."""
    nonce_field = user_object.current_session_token.get_nonce()
    # latest_stamp = nonce_field[18:35]; format = "YYYYMMDDhhmmssfff"
    latest = nonce_field[18:]
    try:
        secs = datetime.strptime(latest[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        ms = int(latest[14:17])
        restamp_ms = int(secs.timestamp() * 1000) + ms
    except Exception:
        # Malformed stamp would be a server-side bug; surface zero so the
        # browser fails the next pre-flight rather than silently extending.
        return 0
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return max(0, math.floor((restamp_ms + 300_000 - now_ms) / 1000))


def _was_registered(user_object) -> bool:
    """Tiny derived flag for REQ-T-010: lets the browser pick the right
    timeout copy without ever needing to read user_object on the wire."""
    return bool(getattr(user_object, "is_registered", False))


def _session_token_wire(user_object) -> dict:
    """Cryptographic-display projection of the session token.

    Surfaces the bare SessionToken — the three display fields the panels
    render (signed token, nonce, GUID) plus the SessionToken envelope
    (origin, signature, created_at, signed, server_env, last_used) needed
    by the existing /session + /verify-token chain. PHI / model-tier user
    state (the full user_object) stays server-side per the S-005 contract.
    """
    st = user_object.current_session_token
    if hasattr(st, "model_dump"):
        return st.model_dump(mode="python", exclude_none=False)
    # Defensive fallback if the in-memory object is already a dict.
    return dict(st)


@app.post("/gate", operation_id="UniversalGate",
          openapi_extra=_impl(
              "AuthorizationsAndAuthenticationsTool + UniversalNavigationTool",
              "../architecture/AuthorizationsAndAuthentications/"
              "universal_navigation_tool.py",
          ))
async def gate(
    request: Request,
    response: Response,
    body: dict | None = Body(default=None),
    ch_session: str | None = Cookie(default=None),
):
    """Single entrance for every client call.

    Returns an NDJSON streaming response when the client sends
    `Accept: application/x-ndjson`. Otherwise returns a single JSON
    object identical to the last (terminal) event emitted.
    """
    payload = dict(body or {})
    op = str(payload.get("op") or "boot")
    op_payload = payload.get("payload") or {}
    intent = payload.get("intent")
    # ch_session cookie value carries the 67-byte assembled session token
    # per REQ-B-007. The first 32 bytes are the GUID.
    prior_guid = None
    if ch_session and len(ch_session) >= 32:
        prior_guid = ch_session[:32]
    elif payload.get("prior_guid"):
        prior_guid = payload.get("prior_guid")

    try:
        # Step 1 — AuthN. Resolves the user_object; does NOT persist (the
        # gate calls AUTHN_TOOL.persist at the very end so all downstream
        # mutations land in a single write).
        authn_deps = AuthnDeps(
            prior_guid=prior_guid,
            server_env=_ENV,
            mongo_frontend=authn.get_mongo_frontend(),
        )
        authn_resp = await AUTHN_TOOL.run(authn_deps, authn.Request(intent=intent))
        user_object = authn_resp.user_object
        fresh_mint = authn_resp.fresh_mint
        guid = user_object.current_session_token.get_auth_token()
        _set_session_cookie(response, user_object)

        # Control-path short-circuit: AuthN signaled a redirect (login_register).
        # Persist the session and return the redirect event; do NOT run nav.
        if authn_resp.redirect_url:
            try:
                await AUTHN_TOOL.persist(authn_deps, user_object, fresh_mint)
            except Exception as exc:
                _log.exception("AuthN.persist (redirect path) failed: %s", exc)
            redirect_event = {
                "type": "redirect",
                "url": authn_resp.redirect_url,
                "time_remaining_seconds": _time_remaining_seconds(user_object),
                "was_registered": _was_registered(user_object),
                "session_token": _session_token_wire(user_object),
            }
            accept = (request.headers.get("accept") or "").lower()
            want_ndjson_short = "application/x-ndjson" in accept or "text/event-stream" in accept
            if want_ndjson_short:
                body_bytes = (_json.dumps(redirect_event, default=str) + "\n").encode("utf-8")
                short_resp = Response(content=body_bytes, media_type="application/x-ndjson")
                _set_session_cookie(short_resp, user_object)
                return short_resp
            return redirect_event

        accept = (request.headers.get("accept") or "").lower()
        want_ndjson = "application/x-ndjson" in accept or "text/event-stream" in accept

        # Iteration 1: buffer events and emit the whole batch as one
        # NDJSON-formatted Response. True progressive streaming is iteration-2
        # work (we tried StreamingResponse here and smoke timed out on the
        # 26s tail of /search; need to debug FE chunk-handling before
        # re-enabling — until then, this guarantees green smoke).
        events: list[dict] = []

        def stream_sink(event: dict) -> None:
            events.append(event)

        deps = AgentDeps(
            user_object=user_object,
            session_token=user_object.current_session_token,
            mongo_frontend=authn_deps.mongo_frontend,
            mongo_pipeline=None,
            server_env=_ENV,
            stream=stream_sink,
        )
        nav_req = nav.Request(op=op, payload=op_payload)

        nav_exc: Optional[BaseException] = None
        res = None
        try:
            res = await UNIVERSAL_NAV_TOOL.run(deps, nav_req)
        except Exception as exc:
            nav_exc = exc
            _log.exception(
                "UniversalNavigation run failed for op=%s payload=%r: %s",
                op, op_payload, exc,
            )

        # REQ-T-008 contract: the full user_object (PHI / model-tier user
        # state) MUST NOT cross the wire on /gate responses. The browser
        # keeps no copy of user_object. The cryptographic-display
        # projection of the session token (signed token, nonce, GUID +
        # the SessionToken envelope) IS surfaced — it is what the
        # session-verification panels (S-003-REQ-B-004) and the
        # cross-service /session + /verify-token chain consume.
        session_token_proj = _session_token_wire(user_object)
        if nav_exc is not None:
            final_event = {
                "kind": "final", "ok": False,
                "error": f"{type(nav_exc).__name__}: {nav_exc}",
                "guid": guid,
                "time_remaining_seconds": _time_remaining_seconds(user_object),
                "was_registered": _was_registered(user_object),
                "session_token": session_token_proj,
            }
        else:
            final_event = {
                "kind": "final", "ok": True,
                "guid": guid,
                "result": res.result if res else {},
                "result_kind": res.kind if res else "unknown",
                "time_remaining_seconds": _time_remaining_seconds(user_object),
                "was_registered": _was_registered(user_object),
                "session_token": session_token_proj,
            }

        # Single persist: write the (possibly-mutated) user_object back to
        # Users.sessions. AuthN is the sole writer; tools never touch Mongo.
        try:
            await AUTHN_TOOL.persist(authn_deps, user_object, fresh_mint)
        except Exception as exc:
            _log.exception("AuthN.persist failed: %s", exc)

        if want_ndjson:
            body_lines = [
                (_json.dumps(e, default=str) + "\n").encode("utf-8") for e in events
            ]
            body_lines.append(
                (_json.dumps(final_event, default=str) + "\n").encode("utf-8"),
            )
            body = b"".join(body_lines)
            ndjson_resp = Response(
                content=body, media_type="application/x-ndjson",
            )
            _set_session_cookie(ndjson_resp, user_object)
            return ndjson_resp

        return final_event
    except Exception as exc:
        # REQ-T-009: any /gate exception MUST log the full stack with the
        # originating request shape. The browser sees HTTP 500 and renders
        # its hard-fail page.
        _log.exception(
            "/gate failed: method=%s path=%s op=%s intent=%r "
            "had_session_cookie=%s body_keys=%s exc=%s: %s",
            request.method, request.url.path, op, intent,
            bool(ch_session), sorted(list(payload.keys())),
            type(exc).__name__, exc,
        )
        raise


# ─────────────────────────────────────────────────────────────────────
# Auxiliary routes (out of scope of this refactor — stay direct)
# ─────────────────────────────────────────────────────────────────────

@app.post("/auth/issue", operation_id="AuthIssue", response_model=SessionToken,
          openapi_extra=_impl("MintableAuthToken", "authentication/mintable_auth_token.py"))
def auth_issue():
    return MintableAuthToken.manufacture(server_env=_ENV).to_wire()


@app.post("/session", operation_id="Session", response_model=SessionToken,
          openapi_extra=_impl("AuthToken", "chathealthy_frontend_lib/authentication/auth_token.py"))
def session(body: SessionRestampRequest):
    return AuthToken.handle_session(body, origin=_ORIGIN, server_env=_ENV)


@app.post("/verify-token", operation_id="VerifyToken", response_model=VerifyTokenResponse,
          openapi_extra=_impl("AuthToken", "chathealthy_frontend_lib/authentication/auth_token.py"))
def verify_token(body: SessionRestampRequest):
    return AuthToken.handle_verify(body, origin=_ORIGIN, server_env=_ENV)


@app.post("/transfer/to-findcare", operation_id="TransferToFindCareEndpoint",
          openapi_extra=_impl("TransferToFindCareEndpoint", "displayChrome/transfer_to_findcare_endpoint.py"))
def transfer_to_findcare():
    return TransferToFindCareEndpoint()()


@app.get("/secrets/{key}", operation_id="SecretsEndpoint",
         openapi_extra=_impl("SecretsEndpoint", "secretsManager/secrets_endpoint.py"))
def get_secret(key: str):
    return SecretsEndpoint()(key)


@app.get("/auth/google/start", operation_id="GoogleOAuthStart",
         openapi_extra=_impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
def google_oauth_start(session_guid: str | None = None):
    return GoogleOAuthEndpoint.start(server_env=_ENV, session_guid=session_guid)


@app.get("/auth/google/callback", operation_id="GoogleOAuthCallback",
         openapi_extra=_impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
def google_oauth_callback(
    code: str = None, state: str = None, error: str = None,
    ch_session: str | None = Cookie(default=None),
):
    return GoogleOAuthEndpoint.callback(
        code=code, state=state, server_env=_ENV,
        session_guid=(ch_session[:32] if ch_session else None), error=error,
    )


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8002"))
    _log.info("SharedServices starting on port %d", port)
    kwargs = {"host": "0.0.0.0", "port": port}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
