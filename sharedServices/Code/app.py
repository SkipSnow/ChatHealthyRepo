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
import os
import sys
import tempfile
import time
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

def _set_session_cookie(response: Response, guid: str) -> None:
    response.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=guid,
        max_age=_SESSION_COOKIE_MAX_AGE,
        httponly=True,
        secure=True,
        samesite="none",
        path="/",
    )


def _do_history_push(mongo_client, guid: str, directive) -> None:
    if directive is None:
        return
    coll = mongo_client["admin"]["sessions"]
    coll.update_one(
        {"_id": guid},
        {"$push": {f"session_conversation_history.{directive.array}": directive.entry}},
    )


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
    prior_guid = ch_session or payload.get("prior_guid")

    # Step 1 — AuthN. Resolves the user_object; does NOT persist (the
    # gate calls AUTHN_TOOL.persist at the very end so all downstream
    # mutations land in a single write).
    authn_deps = AuthnDeps(
        prior_guid=prior_guid,
        server_env=_ENV,
        mongo_frontend=authn.get_mongo_frontend(),
    )
    authn_resp = await AUTHN_TOOL.run(authn_deps)
    user_object = authn_resp.user_object
    fresh_mint = authn_resp.fresh_mint
    guid = user_object.current_session_token.get_auth_token()
    _set_session_cookie(response, guid)

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
        _log.exception("UniversalNavigation run failed for op=%s: %s", op, exc)

    def _user_object_wire() -> dict:
        return user_object.model_dump(mode="python", exclude_none=True)

    if nav_exc is not None:
        final_event = {
            "kind": "final", "ok": False,
            "error": f"{type(nav_exc).__name__}: {nav_exc}",
            "guid": guid, "user_object": _user_object_wire(),
        }
    else:
        final_event = {
            "kind": "final", "ok": True,
            "guid": guid,
            "user_object": _user_object_wire(),
            "result": res.result if res else {},
            "result_kind": res.kind if res else "unknown",
        }

    # Single persist: write the (possibly-mutated) user_object back to
    # admin.sessions. AuthN is the sole writer; tools never touch Mongo.
    try:
        await AUTHN_TOOL.persist(authn_deps, user_object, fresh_mint)
    except Exception as exc:
        _log.warning("AuthN.persist failed: %s", exc)

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
        _set_session_cookie(ndjson_resp, guid)
        return ndjson_resp

    return final_event


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
def google_oauth_start():
    return GoogleOAuthEndpoint.start(server_env=_ENV)


@app.get("/auth/google/callback", operation_id="GoogleOAuthCallback",
         openapi_extra=_impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
def google_oauth_callback(code: str = None, state: str = None, error: str = None):
    return GoogleOAuthEndpoint.callback(code=code, state=state, server_env=_ENV, error=error)


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
