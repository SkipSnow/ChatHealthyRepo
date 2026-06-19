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
from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
import os
import sys
import tempfile
import time

from fastapi import Body, Cookie, FastAPI, Form as FormBody, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

log = ChatHealthyLoggingService()


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def bootstrap_certs_from_env():
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
            raise ChatHealthyException(
                mode="startup_invalid_base64",
                message=f"STARTUP: {env_var} not valid base64: {e}",
                component="SharedServices",
                exception=e,
            )
        os.makedirs(runtime_dir, exist_ok=True)
        path = os.path.join(runtime_dir, filename)
        with open(path, "wb") as f:
            f.write(pem)
        try:
            os.chmod(path, 0o600)
        except Exception as _exc:
            log.warning("STARTUP: chmod 0600 on %s failed (continuing): %s", path, _exc, exc=ChatHealthyException(
                                                                                          mode="startup_chmod_failed",
                                                                                          message=f"STARTUP: chmod 0600 on {path} failed (continuing): {_exc}",
                                                                                          component="SharedServices",
                                                                                          exception=_exc,
                                                                                      ), if_not_debug_log=True)
        wrote.append(filename)
    if wrote:
        os.environ["CERTS_DIR"] = runtime_dir
        log.info("startup bootstrap: wrote %s to %s", ",".join(wrote), runtime_dir)


bootstrap_certs_from_env()

app = FastAPI(title="ChatHealthy.ai Shared Services", version="0.1.5")

import datetime as dt


@app.exception_handler(Exception)
async def fatal(request: Request, exc: Exception):
    log.exception("fatal on %s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"service": "SharedServices", "source": "unhandled",
                 "time": dt.datetime.now(dt.timezone.utc).isoformat()},
    )


class AsgiLogMiddleware:
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
            log.info(
                "%s %s → %d (%dms) from %s",
                scope.get("method", "?"),
                scope.get("path", "?"),
                status_holder["code"], elapsed, xff,
            )


app.add_middleware(AsgiLogMiddleware)

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
AUTHN_TOOL = authn.TOOL
UNIVERSAL_NAV_TOOL = nav.TOOL


ORIGIN = "SharedServices"
ENV = os.getenv("ENV_PREFIX", "dev")
SESSION_COOKIE_NAME = "ch_session"
SESSION_COOKIE_MAX_AGE = 300
SESSION_COOKIE_DOMAIN = "chathealthy.ai"
SESSION_COOKIE_PATH = "/gate"


def impl(cls_name, file_subpath):
    return {
        "x-implementing-class": cls_name,
        "x-implementing-file": f"sharedServices/Code/{file_subpath}",
    }


@app.post("/health", operation_id="HealthEndpoint",
          openapi_extra=impl("HealthEndpoint", "healthcheck/health_endpoint.py"))
def health():
    # v2.2 Part B 7.7 — when the Mongo client is unreachable, return 503
    # (not 200). The Website fetch wrapper at Website/index.html lines
    # 670-688 paints chFatalError on any 503; that turns this endpoint
    # into the visible operator surface that the rotation-as-operational-
    # response model depends on. The JSON body is preserved so the
    # non-prod banner still renders degraded state.
    payload = HealthEndpoint()()
    if payload.get("db") != "connected":
        return JSONResponse(status_code=503, content=payload)
    return payload


# ─────────────────────────────────────────────────────────────────────
# /gate — the universal entrance. Streams NDJSON.
# ─────────────────────────────────────────────────────────────────────

def set_session_cookie(response: Response, cookie_value: str) -> None:
    """REQ-T-001 + REQ-T-002.

    Cookie attributes: Secure, HttpOnly, Max-Age=300, SameSite=None,
    Domain=chathealthy.ai, Path=/gate. Value is the 67-byte session-token
    assembly per REQ-B-007 (assembled inside UniversalNavigationTool's
    orchestrator; this thin wrapper just puts it on the HTTP response).

    The Domain attribute is included on every response regardless of host
    so the header satisfies REQ-T-001 byte-for-byte. Browsers that reach
    the endpoint via a host that does NOT match chathealthy.ai (localhost
    during smoke runs; the bare HF Space hostname) will silently drop the
    cookie. That is expected — production traffic reaches /gate at
    *.chathealthy.ai by way of the Pages Function fronting the HF Space,
    where the Domain attribute lines up with the browser-visible host.
    """
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=cookie_value,
        max_age=SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="none",
        domain=SESSION_COOKIE_DOMAIN,
        path=SESSION_COOKIE_PATH,
    )


@app.post("/gate", operation_id="UniversalGate",
          openapi_extra=impl(
              "AuthorizationsAndAuthenticationsTool + UniversalNavigationTool",
              "AuthorizationsAndAuthentications/"
              "universal_navigation_tool.py",
          ))
async def gate(
    request: Request,
    body: dict | None = Body(default=None),
    ch_session: str | None = Cookie(default=None),
):
    """Single entrance for every client call.

    HTTP plumbing only: parse the POST body + cookies, hand off to
    UniversalNavigationTool.handle_gate for all orchestration, then
    shape the returned GateResponse into a FastAPI response (Streaming,
    bytes-NDJSON, or JSON) and stamp the session cookie.
    """
    payload = dict(body or {})
    op = str(payload.get("op") or "boot")
    op_payload = payload.get("payload") or {}
    intent = payload.get("intent")
    # ch_session cookie value carries the 67-byte assembled session token
    # per REQ-B-007. The first 32 bytes are the GUID.
    prior_guid = None
    prior_guid_source = "none"
    if ch_session and len(ch_session) >= 32:
        prior_guid = ch_session[:32]
        prior_guid_source = "cookie"
    elif payload.get("prior_guid"):
        prior_guid = payload.get("prior_guid")
        prior_guid_source = "body"
    log.debug(
        "/gate ENTRY op=%s intent=%r prior_guid=%s source=%s "
        "(cookie_present=%s body_keys=%s)",
        op, intent,
        (prior_guid[:8] + "..." if prior_guid else None),
        prior_guid_source,
        bool(ch_session),
        sorted(list(payload.keys())),
    )

    accept = (request.headers.get("accept") or "").lower()
    want_ndjson = "application/x-ndjson" in accept or "text/event-stream" in accept

    # Client IP for safety-lockout hydration. X-Forwarded-For wins because
    # Cloudflare and HF proxies put the real client there; bare
    # request.client.host falls back when there's no proxy (local docker).
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    client_ip = xff or (request.client.host if request.client else "")

    try:
        gate_req = nav.GateRequest(
            op=op,
            payload=op_payload,
            intent=intent,
            prior_guid=prior_guid,
            want_ndjson=want_ndjson,
            client_ip=client_ip,
        )
        gate_resp = await UNIVERSAL_NAV_TOOL.handle_gate(gate_req)

        if gate_resp.body_kind == "ndjson_stream":
            resp = StreamingResponse(
                gate_resp.body_data, media_type="application/x-ndjson",
            )
        elif gate_resp.body_kind == "ndjson_bytes":
            resp = Response(
                content=gate_resp.body_data, media_type="application/x-ndjson",
            )
        else:  # "json"
            resp = JSONResponse(content=gate_resp.body_data)

        set_session_cookie(resp, gate_resp.cookie_value)
        return resp
    except Exception as exc:
        # REQ-T-009: any /gate exception MUST log the full stack with the
        # originating request shape. The browser sees HTTP 500 and renders
        # its hard-fail page.
        raise ChatHealthyException(
            mode="gate_failed",
            message=f"/gate failed: method={request.method} path={request.url.path} op={op} intent={intent!r}: {type(exc).__name__}: {exc}",
            component="SharedServices",
            exception=exc,
        )


# ─────────────────────────────────────────────────────────────────────
# Auxiliary routes (out of scope of this refactor — stay direct)
# ─────────────────────────────────────────────────────────────────────

@app.post("/auth/issue", operation_id="AuthIssue", response_model=SessionToken,
          openapi_extra=impl("MintableAuthToken", "authentication/mintable_auth_token.py"))
def auth_issue():
    return MintableAuthToken.manufacture(server_env=ENV).to_wire()


@app.post("/session", operation_id="Session", response_model=SessionToken,
          openapi_extra=impl("AuthToken", "chathealthy_frontend_lib/authentication/auth_token.py"))
def session(body: SessionRestampRequest):
    return AuthToken.handle_session(body, origin=ORIGIN, server_env=ENV)


@app.post("/verify-token", operation_id="VerifyToken", response_model=VerifyTokenResponse,
          openapi_extra=impl("AuthToken", "chathealthy_frontend_lib/authentication/auth_token.py"))
def verify_token(body: SessionRestampRequest):
    return AuthToken.handle_verify(body, origin=ORIGIN, server_env=ENV)


@app.post("/transfer/to-findcare", operation_id="TransferToFindCareEndpoint",
          openapi_extra=impl("TransferToFindCareEndpoint", "displayChrome/transfer_to_findcare_endpoint.py"))
def transfer_to_findcare():
    return TransferToFindCareEndpoint()()


@app.get("/secrets/{key}", operation_id="SecretsEndpoint",
         openapi_extra=impl("SecretsEndpoint", "secretsManager/secrets_endpoint.py"))
def get_secret(key: str):
    return SecretsEndpoint()(key)


@app.post("/auth/google/start", operation_id="GoogleOAuthStart",
          openapi_extra=impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
def google_oauth_start(
    session_guid: str | None = FormBody(default=None),
    flow: str = FormBody(default="login"),
):
    return GoogleOAuthEndpoint.start(
        server_env=ENV, session_guid=session_guid, flow=flow,
    )


@app.get("/fake_google/auth", operation_id="FakeGoogleAuth")
def fake_google_auth(state: str = "", flow: str = "login"):
    from authentication.fake_google_endpoint import serve_auth_page
    return serve_auth_page(state=state, flow=flow)


@app.post("/fake_google/submit", operation_id="FakeGoogleSubmit")
def fake_google_submit(
    email: str = FormBody(...),
    password: str = FormBody(...),
    state: str = FormBody(...),
    flow: str = FormBody("login"),
    create_account: str | None = FormBody(default=None),
    confirm: str | None = FormBody(default=None),
):
    from authentication.fake_google_endpoint import submit_credentials
    final_flow = "register" if create_account == "on" else flow
    return submit_credentials(
        email=email, password=password, state=state,
        flow=final_flow, server_env=ENV,
    )


@app.get("/auth/google/callback", operation_id="GoogleOAuthCallback",
         openapi_extra=impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
def google_oauth_callback(
    code: str = None, state: str = None, error: str = None,
    ch_session: str | None = Cookie(default=None),
):
    return GoogleOAuthEndpoint.callback(
        code=code, state=state, server_env=ENV,
        session_guid=(ch_session[:32] if ch_session else None), error=error,
    )


# ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8002"))
    log.info("SharedServices starting on port %d", port)
    kwargs = {"host": "0.0.0.0", "port": port}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
