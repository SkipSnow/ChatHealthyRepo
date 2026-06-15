# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from fastapi import HTTPException
from pydantic import BaseModel, Field

from chathealthy_frontend_lib.authentication import (
    AuthToken,
    SessionToken,
    SessionTokenVerification,
)


class Provider(BaseModel):
    model_config = {
        "extra": "allow",
        "json_schema_extra": {
            "$ref": "https://dev.chathealthy.ai/schemas/ChatHealthyProvidersSchema.json"
        },
    }


class EvaluatedProvider(BaseModel):
    name: str
    specialty: str
    npi: str


class EvaluateProvidersRequest(BaseModel):
    providers: list[Provider] = Field(..., min_length=1)
    session_token: SessionToken
    question_summary: str = Field(default="")


class EvaluateProvidersResponse(BaseModel):
    status: str
    evaluated_providers: list[EvaluatedProvider]
    question_summary: str
    session_token: SessionTokenVerification


class EvaluateProvidersEndpoint:
    _ORIGIN = "EvaluateCare"

    def __init__(self):
        self.log = ChatHealthyLoggingService()
    def __call__(self, body: EvaluateProvidersRequest) -> EvaluateProvidersResponse:
        at = AuthToken(body.session_token, origin=self._ORIGIN)
        try:
            valid = at.verify()
        except ValueError as e:
            self.log.warning("evaluate/providers verify 400: %s", e, exc=ChatHealthyException(
                                                                      mode="evaluate_providers_verify_400",
                                                                      message=f"evaluate/providers verify 400: {e}",
                                                                      component="EvaluateProvidersEndpoint",
                                                                      exception=e,
                                                                  ), if_not_debug_log=True)
            raise HTTPException(status_code=400, detail=str(e)) from e

        # Provider model is extra='allow' with zero declared fields, so any
        # field the frontend didn't include raises AttributeError on direct
        # access. Use getattr-with-default so the or-fallback chain works.
        results = [
            EvaluatedProvider(
                name=getattr(p, "name", None) or "Unknown",
                specialty=(getattr(p, "specialty", None)
                           or getattr(p, "primary_specialty", None)
                           or "Unknown"),
                npi=getattr(p, "npi", None) or "Unknown",
            )
            for p in body.providers
        ]
        return EvaluateProvidersResponse(
            status="received",
            evaluated_providers=results,
            question_summary=body.question_summary or "Provider evaluation",
            session_token=at.to_verification(valid=valid, server_env=body.session_token.server_env),
        )
