# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SharedServices — FastAPI app on port 8002.
#
# Owner: EPIC-002-F-003 (Authorizations and Authentications).
#
# The transport shell — the app object, the CORS policy, the two exception
# handlers, and the /gate entrance — is the generic Gate in
# chathealthy_lib.gate; it is the one node that knows transport exists. This
# file supplies the component's identity, its navigator, and the auxiliary
# doors that are not yet on /gate (OAuth, secrets, session issuance).
#
# handle_gate is where the work is. It establishes the user through
# authorizations_and_authentications_tool and dispatches the named op to
# its handler, emitting stream events as it runs.
#
# No op is answered here. What an op means -- what a peer is, what health
# is, when a token is valid -- is decided by the navigator, so this file
# holds no opinion about any of it and a new op needs nothing from it.

# Establish which component this process is and what the library will let it
# load, before any other library capability is imported. The finder installed
# here refuses a forbidden module at import, so a late import inside a
# function is caught the same as one at the top of a file -- which only holds
# if nothing has been imported ahead of this call.
from chathealthy_lib.permissions import initialize as _ch_permissions_init
_ch_permissions_init()

import base64
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib import gate as ch_gate
import os
import sys
import tempfile
import time

from fastapi import Form as FormBody, Request
from fastapi.responses import JSONResponse

log = ChatHealthyLoggingService()


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


# This service acts as frontendUser, including when it writes its own
# logs. The Mongo log handler refuses to build without an identity, and
# nothing else in this process sets one.
from chathealthy_lib.logging_service import set_mongo_log_identity
set_mongo_log_identity("frontendUser")

ORIGIN = "SharedServices"
ENV = os.getenv("ENV_PREFIX", "dev")

# The transport shell, once. CORS and the two exception handlers come from
# the generic Gate; SharedServices names its own component and origins. The
# ChatHealthyException response body carries the failure mode so the client
# panel can select its wording from where the failure happened.
app = ch_gate.build_gate_app(
    title="ChatHealthy.ai Shared Services",
    component="SharedServices",
    cors_allow_origins=["https://localhost", "https://localhost:443", "https://localhost:3000",
                        "https://localhost:8080", "https://localhost:8081",
                        "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    cors_allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    cors_allow_credentials=True,
    error_body_includes_mode=True,
)

# Every runtime is rebindable, not only the one that happens to read
# versioned collections today. /admin/swap is how a data version is
# activated, and a service that does not expose it cannot be told which
# collection generation to serve -- so a version activation would silently
# cover part of the application and report success. Mounting the router costs
# nothing where no slot is bound: the endpoint answers and the swap is a
# no-op for a target the binding document does not name.
from chathealthy_lib.runtime_data_collections import (  # noqa: E402
    router as data_collections_router,
)

app.include_router(data_collections_router)

import datetime as dt


# ── Routes ──────────────────────────────────────────────────────────

from healthcheck.health_endpoint import HealthEndpoint
from secretsManager.secrets_endpoint import SecretsEndpoint
from chathealthy_lib.authentication import (
    AuthToken, SessionToken, VerifyTokenResponse,
)
from chathealthy_lib.authentication.user_object import UserObject
from authentication.mintable_auth_token import MintableAuthToken
from chathealthy_lib.authentication.agent_deps import AuthnDeps
from authentication.google_oauth_endpoint import GoogleOAuthEndpoint

# New architecture: two tools chained inside /gate.
from authentication import (
    authorizations_and_authentications_tool as authn,
    universal_navigation_tool as nav,
)
AUTHN_TOOL = authn.TOOL
UNIVERSAL_NAV_TOOL = nav.TOOL


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
        log.error("/health returning 503 — db not connected; payload=%s",
                  payload, extra={"fatal_error": True})
        return JSONResponse(status_code=503, content=payload)
    return payload


# ─────────────────────────────────────────────────────────────────────
# /gate — the universal entrance, wired to this process's navigator. The
# Gate carries transport; the navigator carries the work.
# ─────────────────────────────────────────────────────────────────────
ch_gate.install_gate_route(
    app,
    navigator=UNIVERSAL_NAV_TOOL,
    gate_request_cls=nav.GateRequest,
    origin=ORIGIN,
)


# ─────────────────────────────────────────────────────────────────────
# Auxiliary routes (OAuth + secrets — out of scope of /gate; OAuth needs
# top-level navigation; secrets are admin-only). Everything else is on
# /gate per EPIC-002-F-004-S-001.
# ─────────────────────────────────────────────────────────────────────

@app.post("/auth/issue", operation_id="AuthIssue", response_model=SessionToken,
          openapi_extra=impl("AuthIssueEndpoint",
                             "authentication/auth_issue_endpoint.py"))
async def auth_issue(request: Request):
    from authentication.auth_issue_endpoint import AuthIssueEndpoint
    return await AuthIssueEndpoint()(request)


@app.get("/secrets/{key}", operation_id="SecretsEndpoint",
         openapi_extra=impl("SecretsEndpoint", "secretsManager/secrets_endpoint.py"))
def get_secret(key: str):
    return SecretsEndpoint()(key)


@app.post("/auth/google/start", operation_id="GoogleOAuthStart",
          openapi_extra=impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
async def google_oauth_start(
    session_guid: str | None = FormBody(default=None),
    flow: str = FormBody(default="login"),
):
    return await GoogleOAuthEndpoint.start(
        server_env=ENV, session_guid=session_guid, flow=flow,
    )


@app.get("/auth/google/callback", operation_id="GoogleOAuthCallback",
         openapi_extra=impl("GoogleOAuthEndpoint", "authentication/google_oauth_endpoint.py"))
async def google_oauth_callback(
    code: str = None, state: str = None, error: str = None,
):
    # session_guid is recovered from the HMAC-signed OAuth state parameter
    # (see GoogleOAuthEndpoint.build_state / verify_state). No cookies used.
    return await GoogleOAuthEndpoint.callback(
        code=code, state=state, server_env=ENV, error=error,
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
