# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SharedServices — FastAPI app on port 8002.
# Each FastAPI route constructs the dedicated endpoint class and runs it.
# Endpoint classes live in api/, healthcheck/, externalInterface/.

import os
import sys
import time
import base64
import tempfile
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware

# Code/ on sys.path so api/, healthcheck/, externalInterface/, security/
# all import as top-level packages.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("shared_services")


def _bootstrap_certs_from_env():
    """EPIC-002-F-001-S-012-REQ-T-005: decode PEM certs from HF Space Secrets
    into a runtime directory so session_token.verify_session_token can find
    them on HF. No-op locally where /certs is bind-mounted and CERTS_DIR is
    already set."""
    runtime_dir = os.path.join(tempfile.gettempdir(), "ch_certs")
    mapping = {
        "FINDCARE_CERT_PEM":      "findcare.crt",
        "SHARED_CERT_PEM":        "shared.crt",
        "SHARED_SIGNING_KEY_PEM": "shared.key",
        "CA_CERT_PEM":            "ca.crt",
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
        _log.info("startup bootstrap: wrote %s to %s", ",".join(wrote), runtime_dir)


_bootstrap_certs_from_env()

app = FastAPI(title="ChatHealthy.ai Shared Services", version="0.1.4")

# EPIC-008-F-011-S-001-REQ-B-002 / REQ-B-003 — uniform fatal-error contract.
from runtimeGovernance.runtime_governance import register_fatal_handler
register_fatal_handler(app, service_name="SharedServices")


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = round((time.time() - start) * 1000)
    _log.info(
        "%s %s → %d (%dms) from %s",
        request.method, request.url.path, response.status_code, elapsed,
        request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown"),
    )
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


# ── Routes — each constructs its endpoint class and runs it ──

from healthcheck.health_endpoint import HealthEndpoint
from displayChrome.splash_endpoint import SplashEndpoint
from displayChrome.transfer_to_findcare_endpoint import TransferToFindCareEndpoint
from security.session_endpoint import SessionEndpoint
from security.verify_token_endpoint import VerifyTokenEndpoint, VerifyTokenRequest, VerifyTokenResponse
from secretsManager.secrets_endpoint import SecretsEndpoint
from security.session_token import SessionToken


def _impl(cls_name, file_subpath):
    return {
        "x-implementing-class": cls_name,
        "x-implementing-file": f"sharedServices/Code/{file_subpath}",
    }


@app.get("/health", operation_id="HealthEndpoint",
         openapi_extra=_impl("HealthEndpoint", "healthcheck/health_endpoint.py"))
def health():
    return HealthEndpoint()()


@app.get("/splash", operation_id="SplashEndpoint",
         openapi_extra=_impl("SplashEndpoint", "displayChrome/splash_endpoint.py"))
def splash():
    return SplashEndpoint()()


@app.get("/session", operation_id="SessionEndpoint", response_model=SessionToken,
         openapi_extra=_impl("SessionEndpoint", "security/session_endpoint.py"))
def session():
    return SessionEndpoint()()


@app.post("/verify-token", operation_id="VerifyTokenEndpoint", response_model=VerifyTokenResponse,
          openapi_extra=_impl("VerifyTokenEndpoint", "security/verify_token_endpoint.py"))
def verify_token(body: VerifyTokenRequest):
    return VerifyTokenEndpoint()(body)


@app.post("/transfer/to-findcare", operation_id="TransferToFindCareEndpoint",
          openapi_extra=_impl("TransferToFindCareEndpoint", "displayChrome/transfer_to_findcare_endpoint.py"))
def transfer_to_findcare():
    return TransferToFindCareEndpoint()()


@app.get("/secrets/{key}", operation_id="SecretsEndpoint",
         openapi_extra=_impl("SecretsEndpoint", "secretsManager/secrets_endpoint.py"))
def get_secret(key: str):
    return SecretsEndpoint()(key)


# ── Run ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8002"))
    _log.info("SharedServices starting on port %d", port)
    kwargs = {"host": "0.0.0.0", "port": port}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
