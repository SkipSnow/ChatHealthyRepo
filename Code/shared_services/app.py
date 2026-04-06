# Copyright (c) 2026 Skip Snow. All rights reserved.
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
    allow_origins=["http://localhost", "http://localhost:80", "http://localhost:5173",
                   "http://localhost:8000", "http://localhost:8001",
                   "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"http://localhost(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Health ──────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "service": "shared_services", "version": "0.1.4"}

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
