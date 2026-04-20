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


def _bootstrap_certs_from_env():
    """SEC-HTTPS-001-REQ-016: decode PEM certs from HF Space Secrets into a
    runtime directory so session_token.verify_session_token (which needs
    findcare.crt) can find them on HF. No-op locally where /certs is bind-
    mounted and CERTS_DIR is already set."""
    import base64
    runtime_dir = "/tmp/ch_certs"
    mapping = {
        "FINDCARE_CERT_PEM":        "findcare.crt",
        "SHARED_CERT_PEM":          "shared.crt",
        "SHARED_SIGNING_KEY_PEM":   "shared.key",
        "CA_CERT_PEM":              "ca.crt",
    }
    wrote = []
    for env_var, filename in mapping.items():
        b64 = os.environ.get(env_var)
        if not b64:
            continue
        try:
            pem = base64.b64decode(b64.strip())
        except Exception as e:
            _log.error("STARTUP: %s not valid base64: %s", env_var, e)
            raise
        os.makedirs(runtime_dir, exist_ok=True)
        path = os.path.join(runtime_dir, filename)
        with open(path, "wb") as f:
            f.write(pem)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        wrote.append(filename)
    if wrote:
        os.environ["CERTS_DIR"] = runtime_dir
        _log.info("startup bootstrap: wrote %s to %s (CERTS_DIR=%s)",
                  ",".join(wrote), runtime_dir, runtime_dir)


_bootstrap_certs_from_env()

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

_ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")
_MONGO_CLIENT = None

def _mongo():
    global _MONGO_CLIENT
    if _MONGO_CLIENT is not None:
        return _MONGO_CLIENT
    uri = os.environ.get("MONGO_FRONTEND_connectionString")
    if not uri:
        return None
    try:
        from pymongo import MongoClient
        _MONGO_CLIENT = MongoClient(uri, serverSelectionTimeoutMS=5000)
        return _MONGO_CLIENT
    except Exception as e:
        _log.warning("shared_services /health: MongoClient init failed: %s", e)
        return None

# graph-exempt: health check — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.get("/health")
def health():
    # DEVOPS-DEPLOY-001-REQ-016: read build/version/framework from
    # {ENV_PREFIX}_System.version._id='current'.
    _build = "?"; _version_str = "?"; _framework_str = "?"
    db_status = "unavailable"
    c = _mongo()
    if c is not None:
        try:
            doc = c[f"{_ENV_PREFIX}_System"]["version"].find_one({"_id": "current"}) or {}
            _build = doc.get("build", "?")
            _version_str = doc.get("version", "?")
            _framework_str = doc.get("framework", "?")
            db_status = "connected"
        except Exception as e:
            _log.warning("shared_services /health: MongoDB read failed: %s", e)
    return {"status": "ok", "service": "shared_services",
            "db": db_status, "env": _ENV_PREFIX,
            "build": _build, "version": _version_str, "framework": _framework_str}

# ── Splash + Control Transfer (mirrors EvaluateCare pattern) ──
# graph-exempt: static page render — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.get("/splash")
def splash():
    _log.info("CONTROL TRANSFER: SharedServices has taken ownership of the page")
    return {"html": '<div style="text-align:center;padding:20px;">'
            '<div style="font-size:24px;font-weight:700;color:#1f2937;">Shared Services</div>'
            '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
            '</div>'}

# graph-exempt: proxy/redirect — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
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

# graph-exempt: mTLS/session security primitive, no LLM; per BUG-ARCH-GRAPH-EXEMPT-001
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
# graph-exempt: Key Vault security primitive, no LLM; per BUG-ARCH-GRAPH-EXEMPT-001
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
    kwargs = {"host": "0.0.0.0", "port": port}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
        # mTLS enforcement deferred per BUG-SEC-002 (browser-facing port).
    uvicorn.run(app, **kwargs)
