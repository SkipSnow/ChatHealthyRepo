# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import logging
from typing import Optional
from pydantic import BaseModel

from security.session_token import SessionToken, SessionTokenVerification, verify_session_token


class VerifyTokenRequest(BaseModel):
    session_token: Optional[SessionToken] = None


class VerifyTokenResponse(BaseModel):
    status: str
    session_token: SessionTokenVerification


class VerifyTokenEndpoint:
    """POST /verify-token — verify a FindCare-signed session token. Returns
    a SessionTokenVerification audit + verdict object."""

    def __init__(self):
        self.log = logging.getLogger("shared_services.verify_token")

    def __call__(self, body: VerifyTokenRequest) -> VerifyTokenResponse:
        token_valid = False
        token_origin = "unknown"
        token_str = ""
        sig_str = ""
        if body.session_token:
            inbound = body.session_token.model_dump()
            try:
                token_valid = verify_session_token(inbound, "FindCare")
                token_origin = inbound.get("origin", "unknown")
                self.log.info(
                    "SharedServices token verification: origin=%s valid=%s",
                    token_origin, token_valid,
                )
            except Exception as e:
                self.log.warning("SharedServices token verification failed: %s", e)
            token_str = inbound.get("token", "") or ""
            sig = inbound.get("signature", "") or ""
            sig_str = (sig[:40] + "...") if sig else "none"
        return VerifyTokenResponse(
            status="verified" if token_valid else "failed",
            session_token=SessionTokenVerification(
                token_received=token_str,
                signature_received=sig_str if sig_str else "none",
                # EPIC-002-F-001-S-012-REQ-B-010: origin field self-identifies
                # the responding service (this one), distinct from the
                # cryptographic signer named in token_origin.
                origin="SharedServices",
                verified=token_valid,
            ),
        )
