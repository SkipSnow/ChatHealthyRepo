# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os
import traceback

from security.session_token import SessionToken, verify_session_token, CERTS_DIR as MODULE_CERTS_DIR


class DebugVerifyLiveEndpoint:
    """POST /debug/verify-live — runs verify_session_token on a posted
    SessionToken and returns structured diagnostic. Gated by DEBUG=1."""

    def __init__(self):
        self.enabled = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

    def __call__(self, body: SessionToken):
        if not self.enabled:
            return {"disabled": "DEBUG not set"}
        result = {"verified": None, "error": None, "details": {}}
        try:
            inbound = body.model_dump()
            runtime_certs_dir = os.environ.get("CERTS_DIR", "<unset>")
            cert_path = os.path.join(runtime_certs_dir, "findcare.crt")
            result["details"]["module_CERTS_DIR"] = MODULE_CERTS_DIR
            result["details"]["runtime_CERTS_DIR"] = runtime_certs_dir
            result["details"]["cert_path"] = cert_path
            result["details"]["cert_exists"] = os.path.exists(cert_path)
            if os.path.exists(cert_path):
                result["details"]["cert_size"] = os.path.getsize(cert_path)
                with open(cert_path, "rb") as f:
                    head = f.read(30)
                result["details"]["cert_head"] = head.decode("ascii", errors="replace")
            result["details"]["token_signed"] = inbound.get("signed")
            result["details"]["token_origin"] = inbound.get("origin")
            result["details"]["token_len"] = len(inbound.get("token", ""))
            result["details"]["sig_len"] = len(inbound.get("signature", ""))
            result["verified"] = verify_session_token(inbound, "FindCare")
        except Exception as e:
            result["error"] = f"{type(e).__name__}: {e}"
            result["tb"] = traceback.format_exc()
        return result
