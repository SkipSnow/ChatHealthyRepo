# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os
import traceback

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.authentication import AuthToken, SessionToken
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()


class DebugVerifyLiveEndpoint:
    def __init__(self):
        self.enabled = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

    def __call__(self, body: SessionToken):
        if not self.enabled:
            return {"disabled": "DEBUG not set"}
        result = {"verified": None, "error": None, "details": {}}
        try:
            runtime_certs_dir = os.environ.get("CERTS_DIR", "<unset>")
            cert_path = os.path.join(runtime_certs_dir, "findcare.crt")
            result["details"]["runtime_CERTS_DIR"] = runtime_certs_dir
            result["details"]["cert_path"] = cert_path
            result["details"]["cert_exists"] = os.path.exists(cert_path)
            if os.path.exists(cert_path):
                result["details"]["cert_size"] = os.path.getsize(cert_path)
                with open(cert_path, "rb") as f:
                    head = f.read(30)
                result["details"]["cert_head"] = head.decode("ascii", errors="replace")
            result["details"]["token_signed"] = body.signed
            result["details"]["token_origin"] = body.origin
            result["details"]["token_len"] = len(body.token or "")
            result["details"]["sig_len"] = len(body.signature or "")
            at = AuthToken(body, origin="EvaluateCare")
            result["verified"] = at.verify()
        except Exception as e:
            log.warning("debug_verify_live verify failed: %s", e, exc=ChatHealthyException(
                                                                   mode="debug_verify_live_failed",
                                                                   message=f"debug_verify_live verify failed: {e}",
                                                                   component="DebugVerifyLiveEndpoint",
                                                                   exception=e,
                                                               ), if_not_debug_log=True)
            result["error"] = f"{type(e).__name__}: {e}"
            result["tb"] = traceback.format_exc()
        return result
