# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The /auth/issue endpoint, drained out of app.py.

Session establishment -- the one unauthenticated door in the system, and the
whole of it: the session exists when this returns. The app.py route collects
the request and hands it here; the resume-or-mint work lives in this class so
the route carries transport and nothing else.
"""
from __future__ import annotations

import datetime as dt
import os

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.authentication.agent_deps import AuthnDeps
from chathealthy_lib.authentication.user_object import UserObject

from authentication import authorizations_and_authentications_tool as authn
from authentication.mintable_auth_token import MintableAuthToken

log = ChatHealthyLoggingService()

ENV = os.getenv("ENV_PREFIX", "dev")


class AuthIssueEndpoint:
    """Establish a session and hand back its token.

    The page passes back the GUID it holds. A GUID naming a session that is
    in Mongo and has not expired is resumed; anything else is ignored and a
    new session begins, so a GUID a caller invents buys nothing. The form
    factor is told to the session here because here is where the session is
    made, and it does not change while the session lives.
    """

    async def __call__(self, request) -> dict:
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - an unparsable body is simply no guid
            body = {}
        offered = str((body or {}).get("session_guid") or "").strip()
        resumed = self._live_session_guid(offered) if offered else ""
        if resumed:
            # A session we already have is not authorised again: it is
            # stamped. The GUID is per session and the nonce is per hop, so
            # the token is minted fresh against the session that exists.
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
            deps,
            authn.Request(intent="manufacture_session",
                          user_object=user_object))
        await authn.TOOL.persist(deps, resp.user_object, resp.fresh_mint)
        return resp.user_object.current_session_token.model_dump(mode="json")

    @staticmethod
    def _live_session_guid(guid: str) -> str:
        """The guid, if it names a session that exists and has not expired.
        Returns "" otherwise, and the caller mints."""
        from datetime import datetime, timezone as _tz
        try:
            coll = authn.get_mongo_frontend()[
                authn.SESSION_DB][authn.SESSION_COLLECTION]
            doc = coll.find_one({"_id": guid}, {"expires_at": 1})
        except Exception as exc:  # noqa: BLE001 - unreadable session, mint
            log.info("auth/issue could not read session %s: %s",
                     guid[:8], exc)
            return ""
        if not doc:
            return ""
        expires = doc.get("expires_at")
        if isinstance(expires, str):
            try:
                expires = datetime.fromisoformat(
                    expires.replace("Z", "+00:00"))
            except ValueError:
                return ""
        if isinstance(expires, datetime):
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=_tz.utc)
            if expires <= datetime.now(_tz.utc):
                return ""
        return guid
