# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import logging
from typing import Optional
from fastapi import HTTPException
from pydantic import BaseModel

from chathealthy_frontend_lib.session_token import (
    SessionToken,
    SessionTokenVerification,
    TokenInfraError,
    verify_session_token,
)


class VerifyTokenRequest(BaseModel):
    session_token: Optional[SessionToken] = None


class VerifyTokenResponse(BaseModel):
    status: str
    session_token: SessionTokenVerification


class VerifyTokenEndpoint:
    """POST /verify-token — verify a FindCare-signed session token.

    Failure semantics (NO silent fallbacks):
      - Body shape is bad (missing/empty session_token)        → 400
      - Token contents are malformed (caller-side ValueError)  → 400
      - Server can't complete verification (TokenInfraError —
        cert missing, CERTS_DIR unreadable, malformed cert)    → 500
      - Signature was checked and didn't match                 → 200, verified=False
    """

    def __init__(self):
        self.log = logging.getLogger("evaluate_care.verify_token")

    def __call__(self, body: VerifyTokenRequest) -> VerifyTokenResponse:
        if not body.session_token:
            raise HTTPException(status_code=400, detail="session_token is required")
        inbound = body.session_token.model_dump()
        token_str = inbound.get("token", "") or ""
        sig = inbound.get("signature", "") or ""
        sig_str = (sig[:40] + "...") if sig else "none"

        try:
            token_valid = verify_session_token(inbound, "FindCare")
        except ValueError as e:
            # Caller's input is malformed — surface as 400.
            self.log.warning("EvalCare token verification 400: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e
        # TokenInfraError intentionally NOT caught — propagates as 500
        # so the operator sees the infrastructure failure (cert missing,
        # CERTS_DIR wrong, etc.) instead of a misleading verified=false.

        token_origin = inbound.get("origin", "unknown")
        self.log.info(
            "EvalCare token verification: origin=%s valid=%s",
            token_origin, token_valid,
        )
        return VerifyTokenResponse(
            status="verified" if token_valid else "failed",
            session_token=SessionTokenVerification(
                token_received=token_str,
                signature_received=sig_str,
                # EPIC-002-F-001-S-012-REQ-B-010: origin field self-identifies
                # the responding service (this one), distinct from the
                # cryptographic signer named in token_origin.
                origin="EvaluateCare",
                verified=token_valid,
                created_at=inbound.get("created_at", "") or "",
                server_env=inbound.get("server_env"),
            ),
        )
