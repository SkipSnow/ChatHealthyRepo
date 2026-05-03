# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import logging
from fastapi import HTTPException
from pydantic import BaseModel, Field

from security.session_token import (
    SessionToken,
    SessionTokenVerification,
    TokenInfraError,
    verify_session_token,
)


class Provider(BaseModel):
    """Provider record. Canonical shape lives at the published schema URL —
    https://dev.chathealthy.ai/schemas/ChatHealthyProvidersSchema.json (see also
    Website/schemas/ChatHealthyProvidersSchema.json on disk). The catalog
    references that schema as the source of truth; this Pydantic class is a
    permissive carrier so the canonical schema controls validation, not a
    hand-mirrored Python definition."""
    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "$ref": "https://dev.chathealthy.ai/schemas/ChatHealthyProvidersSchema.json"
        },
    }


class EvaluatedProvider(BaseModel):
    """The post-receive shape EvaluateCare currently echoes back per provider
    (3-field stub until real scoring lands; will return enriched Provider
    records conforming to the canonical schema once implemented)."""
    name: str
    specialty: str
    npi: str


class EvaluateProvidersRequest(BaseModel):
    """Payload posted by FindCare's React iframe (via the FindCare /evaluate/providers
    proxy) when the user clicks Evaluate."""
    providers: list[Provider] = Field(..., min_length=1, description="Selected providers — at least one required; items conform to the canonical Provider schema")
    session_token: SessionToken = Field(..., description="FindCare-signed session token for mutual authentication")
    question_summary: str = Field(default="", description="Free-text reason the user wants these providers evaluated")


class EvaluateProvidersResponse(BaseModel):
    status: str = Field(..., description="`received` on success — set even when no real evaluation is performed (current implementation echoes only)")
    evaluated_providers: list[EvaluatedProvider]
    question_summary: str
    session_token: SessionTokenVerification


class EvaluateProvidersEndpoint:
    """POST /evaluate/providers — external interface from FindCare. Verifies the
    inbound FindCare-signed session token and echoes the providers back. Real
    scoring is not yet implemented; this preserves the current wire contract."""

    def __init__(self):
        self.log = logging.getLogger("evaluate_care.evaluate_providers")

    def __call__(self, body: EvaluateProvidersRequest) -> EvaluateProvidersResponse:
        inbound = body.session_token.model_dump()
        token_str = inbound.get("token", "") or ""
        sig = inbound.get("signature", "") or ""
        sig_str = (sig[:40] + "...") if sig else "none"

        # NO silent fallback: ValueError on caller-side garbage → 400.
        # TokenInfraError on server-side cert/CERTS_DIR failure propagates
        # → 500 (the operator must see infra failure, not a fake
        # verified=False).
        try:
            token_valid = verify_session_token(inbound, "FindCare")
        except ValueError as e:
            self.log.warning("evaluate/providers verify 400: %s", e)
            raise HTTPException(status_code=400, detail=str(e)) from e

        token_origin = inbound.get("origin", "unknown")
        self.log.info(
            "Session token: origin=%s valid=%s token=%s",
            token_origin, token_valid, token_str[:20],
        )

        results = [
            EvaluatedProvider(
                name=p.name or "Unknown",
                specialty=(p.specialty or p.primary_specialty or "Unknown"),
                npi=p.npi or "Unknown",
            )
            for p in body.providers
        ]
        question = body.question_summary or "Provider evaluation"
        self.log.info(
            "Received %d providers from FindCare (token_valid=%s): %s",
            len(results), token_valid, question,
        )
        return EvaluateProvidersResponse(
            status="received",
            evaluated_providers=results,
            question_summary=question,
            session_token=SessionTokenVerification(
                token_received=token_str,
                signature_received=sig_str,
                origin=token_origin,
                verified=token_valid,
            ),
        )
