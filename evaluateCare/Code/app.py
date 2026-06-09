# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EvaluateCare Service — FastAPI app on port 8001.
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
from fastapi.responses import JSONResponse

# Code/ on sys.path so api/, healthcheck/, externalInterface/, security/
# all import as top-level packages.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("evaluate_care")


def _bootstrap_certs_from_env():
    """EPIC-002-F-001-S-012-REQ-T-005: decode PEM certs from HF Space Secrets
    into a runtime directory so SessionToken.verify can find
    them on HF. No-op locally where /certs is bind-mounted and CERTS_DIR is
    already set."""
    runtime_dir = os.path.join(tempfile.gettempdir(), "ch_certs")
    mapping = {
        "FINDCARE_CERT_PEM":        "findcare.crt",
        "EVALCARE_CERT_PEM":        "evalcare.crt",
        "EVALCARE_SIGNING_KEY_PEM": "evalcare.key",
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
        _log.info("startup bootstrap: wrote %s to %s", ",".join(wrote), runtime_dir)


_bootstrap_certs_from_env()

app = FastAPI(title="ChatHealthy.ai EvaluateCare", version="0.1.4")

import datetime as _dt


@app.exception_handler(Exception)
async def _fatal(request: Request, exc: Exception):
    _log.exception("fatal on %s", request.url.path)
    return JSONResponse(
        status_code=503,
        content={"service": "EvaluateCare", "source": "unhandled",
                 "time": _dt.datetime.now(_dt.timezone.utc).isoformat()},
    )


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
    allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$|https://skipsnow-[a-zA-Z0-9-]+\.hf\.space$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes — each constructs its endpoint class and runs it ──

from healthcheck.health_endpoint import HealthEndpoint
from displayChrome.splash_endpoint import SplashEndpoint
from displayChrome.transfer_to_findcare_endpoint import TransferToFindCareEndpoint
from security.debug_verify_live_endpoint import DebugVerifyLiveEndpoint
from security.debug_bootstrap_endpoint import DebugBootstrapEndpoint
from externalInterface.evaluate_providers_endpoint import (
    EvaluateProvidersEndpoint,
    EvaluateProvidersRequest,
    EvaluateProvidersResponse,
)
from chathealthy_frontend_lib.authentication import (
    AuthToken, SessionRestampRequest, SessionToken, VerifyTokenResponse,
)

_ORIGIN = "EvaluateCare"
_ENV = os.getenv("ENV_PREFIX", "dev")


def _impl(cls_name, file_subpath):
    return {
        "x-implementing-class": cls_name,
        "x-implementing-file": f"evaluateCare/Code/{file_subpath}",
    }


@app.post("/health", operation_id="HealthEndpoint",
          openapi_extra=_impl("HealthEndpoint", "healthcheck/health_endpoint.py"))
def health():
    return HealthEndpoint()()


@app.post("/splash", operation_id="SplashEndpoint",
          openapi_extra=_impl("SplashEndpoint", "displayChrome/splash_endpoint.py"))
def splash():
    return SplashEndpoint()()


@app.post("/session", operation_id="Session", response_model=SessionToken,
          openapi_extra=_impl("AuthToken", "chathealthy_frontend_lib/authentication/auth_token.py"))
def session(body: SessionRestampRequest):
    return AuthToken.handle_session(body, origin=_ORIGIN, server_env=_ENV)


@app.post("/verify-token", operation_id="VerifyToken", response_model=VerifyTokenResponse,
          openapi_extra=_impl("AuthToken", "chathealthy_frontend_lib/authentication/auth_token.py"))
def verify_token(body: SessionRestampRequest):
    return AuthToken.handle_verify(body, origin=_ORIGIN, server_env=_ENV)


@app.post("/evaluate/providers", operation_id="EvaluateProvidersEndpoint", response_model=EvaluateProvidersResponse,
          openapi_extra=_impl("EvaluateProvidersEndpoint", "externalInterface/evaluate_providers_endpoint.py"))
def evaluate_providers(body: EvaluateProvidersRequest):
    return EvaluateProvidersEndpoint()(body)


@app.post("/transfer/to-findcare", operation_id="TransferToFindCareEndpoint",
          openapi_extra=_impl("TransferToFindCareEndpoint", "displayChrome/transfer_to_findcare_endpoint.py"))
def transfer_to_findcare():
    return TransferToFindCareEndpoint()()


@app.post("/debug/verify-live", operation_id="DebugVerifyLiveEndpoint",
          openapi_extra=_impl("DebugVerifyLiveEndpoint", "security/debug_verify_live_endpoint.py"))
def debug_verify_live(body: SessionToken):
    return DebugVerifyLiveEndpoint()(body)


@app.get("/debug/bootstrap", operation_id="DebugBootstrapEndpoint",
         openapi_extra=_impl("DebugBootstrapEndpoint", "security/debug_bootstrap_endpoint.py"))
def debug_bootstrap():
    return DebugBootstrapEndpoint()()


# ── Run ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    _log.info("EvaluateCare starting on port %d", port)
    kwargs = {"host": "0.0.0.0", "port": port}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
