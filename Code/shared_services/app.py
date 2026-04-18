# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Shared Services — FastAPI app on port 8002.
# Independent service providing cross-cutting infrastructure:
# SafetyService, ConsentService, LeadService, UnknownQuestionService,
# AboutService, URLGuardian, ChatHealthyMongoUtilities, SecretManager.
#
# GOV-005: This is infrastructure, not a 5th business application.
# EPIC-4: mTLS required for all callers (FindCare, EvaluateCare).

import os
import sys
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("shared_services")

app = FastAPI(title="ChatHealthy.ai Shared Services", version="0.1.4")

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = round((time.time() - start) * 1000)
    _log.info("%s %s → %d (%dms) from %s",
              request.method, request.url.path, response.status_code, elapsed,
              request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown"))
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://localhost", "https://localhost:443", "https://localhost:3000",
                   "https://localhost:8080", "https://localhost:8081",
                   "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health ──────────────────────────────────────────────────
def _commit_label():
    c = os.getenv("COMMIT_SHA", "")
    if not c:
        try:
            c = open(os.path.join(os.path.dirname(__file__), ".commit_sha")).read().strip()
        except Exception:
            c = "?"
    return (c or "?")[:12]

@app.get("/health")
def health():
    return {"status": "ok", "service": "shared_services", "version": "0.1.4",
            "commit": _commit_label()}

# ── Splash + Control Transfer (mirrors EvaluateCare pattern) ──
@app.get("/splash")
def splash():
    _log.info("CONTROL TRANSFER: SharedServices has taken ownership of the page")
    return {"html": '<div style="text-align:center;padding:20px;">'
            '<div style="font-size:24px;font-weight:700;color:#1f2937;">Shared Services</div>'
            '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
            '</div>'}

@app.post("/transfer/to-findcare")
def transfer_to_findcare():
    """SharedServices releases page ownership back to FindCare."""
    _log.info("CONTROL TRANSFER: SharedServices → FindCare")
    return {"owner": "findcare", "reason": "filter_interaction"}

# ── Token Verification (mirrors EvaluateCare pattern) ──────
from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional

class VerifyTokenRequest(_BaseModel):
    session_token: _Optional[dict] = None

@app.post("/verify-token")
def verify_token(body: VerifyTokenRequest):
    """Verify a session token from FindCare. Proves mutual authentication."""
    token_valid = False
    token_origin = "unknown"
    if body.session_token:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
            os.environ.setdefault("CERTS_DIR", os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs"))
            from session_token import verify_session_token
            token_valid = verify_session_token(body.session_token, "FindCare")
            token_origin = body.session_token.get("origin", "unknown")
            _log.info("Token verification: origin=%s valid=%s", token_origin, token_valid)
        except Exception as e:
            _log.warning("Token verification failed: %s", e)
    return {
        "status": "verified" if token_valid else "failed",
        "session_token": {
            "token_received": body.session_token.get("token", "") if body.session_token else "",
            "signature_received": (body.session_token.get("signature", "") or "")[:40] + "..." if body.session_token and body.session_token.get("signature") else "none",
            "origin": body.session_token.get("origin", "") if body.session_token else "",
            "verified": token_valid,
        },
    }

# ── SecretManager (local mode) ─────────────────────────────
@app.get("/secrets/{key}")
def get_secret(key: str):
    """Stub — returns environment variable value.
    Production: reads from Azure Key Vault via x509 cert."""
    value = os.getenv(key)
    if value:
        return {"key": key, "found": True}
    return {"key": key, "found": False, "error": "Secret not found"}

# ── Run ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8002"))
    _log.info("Shared Services starting on port %d", port)
    uvicorn.run("app:app", host="0.0.0.0", port=port)
