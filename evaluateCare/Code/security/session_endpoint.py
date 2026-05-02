# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os
import logging

from security.session_token import SessionToken, generate_session_token


class SessionEndpoint:
    """GET /session — mint an EvaluateCare-signed session token.

    NO FALLBACK. If evalcare.key is missing or generation fails, propagate
    so the caller halts rather than running with a placeholder.
    SEC-HTTPS-001-REQ-021.
    """

    def __init__(self):
        self.log = logging.getLogger("evaluate_care.session")
        self.env_prefix = os.getenv("ENV_PREFIX", "dev")

    def __call__(self) -> SessionToken:
        raw = generate_session_token("EvaluateCare")
        # EPIC-002-F-001-S-012-REQ-B-010: server-asserted env from this container.
        raw["server_env"] = self.env_prefix
        token = SessionToken(**raw)
        self.log.info(
            "Minted EvaluateCare session token: env=%s nonce_len=%d",
            self.env_prefix, len(token.token),
        )
        return token
