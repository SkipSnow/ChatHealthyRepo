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
          openapi_extra=impl("MintableAuthToken", "authentication/mintable_auth_token.py"))
async def auth_issue(request: Request):
    """Establish a session and hand back its token.

    This is the one unauthenticated door in the system, and the whole of
    session establishment: the session exists when this returns. It used
    to mint a token and nothing else, leaving the session to be created
    by a `boot` call that followed it on every page load -- two round
    trips where the second existed only because the first had made a GUID
    with nothing behind it.

    The page passes back the GUID it holds. A GUID naming a session that
    is in Mongo and has not expired is resumed; anything else is ignored
    and a new session begins, so a GUID a caller invents buys nothing.

    The form factor is told to the session here because here is where the
    session is made, and it does not change while the session lives.
    """
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - an unparsable body is simply no guid
        body = {}
    offered = str((body or {}).get("session_guid") or "").strip()
    resumed = _live_session_guid(offered) if offered else ""
    if resumed:
        # A session we already have is not authorised again: it is
        # stamped. The GUID is per session and the nonce is per hop, so
        # the token is minted fresh against the session that exists --
        # handing back the stored one would replay a nonce, and building
        # a second session would orphan the first, which is the waste
        # this endpoint was meant to end.
        return MintableAuthToken.manufacture(
            server_env=ENV, guid=resumed).to_wire()

    reported = str((body or {}).get("form_factor") or "").strip().lower()
    deps = AuthnDeps(session_guid="", server_env=ENV,
                           mongo_frontend=authn.get_mongo_frontend())
    user_object = UserObject(
        current_session_token="NULL",
        expires_at=dt.datetime.now(dt.timezone.utc)
        + dt.timedelta(seconds=authn.SESSION_TTL_SECONDS),
    )
    if reported in ("phone", "desktop"):
        user_object.form_factor = reported
    resp = await authn.TOOL.run(
        deps, authn.Request(intent="manufacture_session", user_object=user_object))
    await authn.TOOL.persist(deps, resp.user_object, resp.fresh_mint)
    return resp.user_object.current_session_token.model_dump(mode="json")


def _live_session_guid(guid: str) -> str:
    """The guid, if it names a session that exists and has not expired.

    Returns "" otherwise, and the caller mints. A session this cannot read
    is not resumed: continuing on a GUID whose session is unknown would hand
    the caller a token for state nobody can produce.
    """
    from datetime import datetime, timezone as _tz
    try:
        coll = authn.get_mongo_frontend()[authn.SESSION_DB][authn.SESSION_COLLECTION]
        doc = coll.find_one({"_id": guid}, {"expires_at": 1})
    except Exception as exc:  # noqa: BLE001 - unreadable session, mint instead
        log.info("auth/issue could not read session %s: %s", guid[:8], exc)
        return ""
    if not doc:
        return ""
    expires = doc.get("expires_at")
    if isinstance(expires, str):
        try:
            expires = datetime.fromisoformat(expires.replace("Z", "+00:00"))
        except ValueError:
            return ""
    if isinstance(expires, datetime):
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=_tz.utc)
        if expires <= datetime.now(_tz.utc):
            return ""
    return guid


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
