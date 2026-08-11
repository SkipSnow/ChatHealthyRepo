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
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# Code/ on sys.path so api/, healthcheck/, externalInterface/, security/
# all import as top-level packages.

log = ChatHealthyLoggingService()


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
def _decode_cert_pem(env_var: str, b64: str, component: str) -> bytes:
    """Decode one PEM from base64. Raises, never logs.

    Extracted so bootstrap_certs_from_env can keep its operational logging
    without also being a raising function -- Rule-005 statement 3: the
    thrower does not log, the catcher does.
    """
    try:
        return base64.b64decode(b64.strip())
    except Exception as e:
        raise ChatHealthyException(
            mode="startup_invalid_base64",
            message=f"STARTUP: {env_var} not valid base64: {e}",
            component=component,
            exception=e,
        )


def bootstrap_certs_from_env():
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
        pem = _decode_cert_pem(env_var, b64, "EvaluateCare")
        os.makedirs(runtime_dir, exist_ok=True)
        path = os.path.join(runtime_dir, filename)
        with open(path, "wb") as f:
            f.write(pem)
        try:
            os.chmod(path, 0o600)
        except Exception as _exc:
            # Mode 1 (REQ-B-008): best-effort startup chmod; system continues.
            log.info("STARTUP: chmod 0600 on %s failed (continuing): %s", path, _exc, exc=ChatHealthyException(
                                                                                          mode="startup_chmod_failed",
                                                                                          message=f"STARTUP: chmod 0600 on {path} failed (continuing): {_exc}",
                                                                                          component="EvaluateCare",
                                                                                          exception=_exc,
                                                                                      ), if_not_debug_log=True)
        wrote.append(filename)
    if wrote:
        os.environ["CERTS_DIR"] = runtime_dir
        log.info("startup bootstrap: wrote %s to %s", ",".join(wrote), runtime_dir)



# This service acts as frontendUser, including when it writes its own
# logs. The Mongo log handler refuses to build without an identity, and
# nothing else in this process sets one.
from chathealthy_lib.logging_service import set_mongo_log_identity
set_mongo_log_identity("frontendUser")
bootstrap_certs_from_env()

app = FastAPI(title="ChatHealthy.ai EvaluateCare", version="0.1.4")

import datetime as dt



@app.exception_handler(Exception)
async def fatal(request: Request, exc: Exception):
    # Safety net for UNHANDLED exceptions per EPIC-008-F-002-S-009-REQ-B-008
    # Mode 3 (unhandled, not expected). Reaching here is always user-fatal
    # (503 to the user) — that IS the Mode 3 definition — so tag fatal_error
    # True. The architectural goal is for Mode 3 occurrences to be RARE; each
    # one observed in the log MUST be moved to a local catch with Mode 1 or
    # Mode 2 handling.
    log.exception("unhandled exception on %s", request.url.path,
                  extra={"fatal_error": True})
    return JSONResponse(
        status_code=503,
        content={"service": "EvaluateCare", "source": "unhandled",
                 "time": dt.datetime.now(dt.timezone.utc).isoformat()},
    )


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    elapsed = round((time.time() - start) * 1000)
    log.info(
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
from chathealthy_lib.authentication import (
    AuthToken, SessionRestampRequest, SessionToken, VerifyTokenResponse,
)

ORIGIN = "EvaluateCare"
ENV = os.getenv("ENV_PREFIX", "dev")


def impl(cls_name, file_subpath):
    return {
        "x-implementing-class": cls_name,
        "x-implementing-file": f"evaluateCare/Code/{file_subpath}",
    }


@app.post("/health", operation_id="HealthEndpoint",
          openapi_extra=impl("HealthEndpoint", "healthcheck/health_endpoint.py"))
def health():
    # v2.2 Part B 7.6/7.7 — return 503 (not 200) when Mongo is
    # unreachable so the Website fetch wrapper triggers chFatalError.
    payload = HealthEndpoint()()
    if payload.get("db") != "connected":
        log.error("/health returning 503 — db not connected; payload=%s",
                  payload, extra={"fatal_error": True})
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.post("/splash", operation_id="SplashEndpoint",
          openapi_extra=impl("SplashEndpoint", "displayChrome/splash_endpoint.py"))
def splash():
    return SplashEndpoint()()


@app.post("/evaluate/providers", operation_id="EvaluateProvidersEndpoint", response_model=EvaluateProvidersResponse,
          openapi_extra=impl("EvaluateProvidersEndpoint", "externalInterface/evaluate_providers_endpoint.py"))
def evaluate_providers(body: EvaluateProvidersRequest):
    return EvaluateProvidersEndpoint()(body)


@app.post("/debug/verify-live", operation_id="DebugVerifyLiveEndpoint",
          openapi_extra=impl("DebugVerifyLiveEndpoint", "security/debug_verify_live_endpoint.py"))
def debug_verify_live(body: SessionToken):
    return DebugVerifyLiveEndpoint()(body)


@app.get("/debug/bootstrap", operation_id="DebugBootstrapEndpoint",
         openapi_extra=impl("DebugBootstrapEndpoint", "security/debug_bootstrap_endpoint.py"))
def debug_bootstrap():
    return DebugBootstrapEndpoint()()


# ── Run ─────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    log.info("EvaluateCare starting on port %d", port)
    kwargs = {"host": "0.0.0.0", "port": port}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
