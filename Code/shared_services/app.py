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
#
# v4-043: every route handler routes business logic through a LangGraph
# StateGraph (compiled once at module load, invoked from the handler body).
# This file's graphs are registered in langgraph.json so Studio renders
# them and LangSmith traces every invocation.

import os
import sys
import logging
import time

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from typing_extensions import TypedDict
from typing import Optional as _Optional
from pydantic import BaseModel as _BaseModel
from langgraph.graph import StateGraph, START, END

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

# ── Module-level service helpers ────────────────────────────

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


# ──────────────────────────────────────────────────────────────────────
# v4-043 LangGraph wrappers — one StateGraph per FastAPI route handler.
# Each graph compiles at module load and is registered in langgraph.json
# so LangGraph Studio renders it and LangSmith traces every invocation.
# ──────────────────────────────────────────────────────────────────────


def _make_single_node_graph(node_name, fn, state_class):
    """Compile a one-node StateGraph: START → <node_name> → END."""
    g = StateGraph(state_class)
    g.add_node(node_name, fn)
    g.add_edge(START, node_name)
    g.add_edge(node_name, END)
    return g.compile()


# ── /health ──────────────────────────────────────────────────────────


class HealthState(TypedDict, total=False):
    response: dict


def _health_node(state: HealthState) -> dict:
    """DEVOPS-DEPLOY-001-REQ-016: read build/version/framework from
    {ENV_PREFIX}_System.version._id='current'."""
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
    return {"response": {
        "status": "ok", "service": "shared_services",
        "db": db_status, "env": _ENV_PREFIX,
        "build": _build, "version": _version_str, "framework": _framework_str,
    }}


health_graph = _make_single_node_graph("health_check", _health_node, HealthState)


@app.get("/health")
def health():
    return health_graph.invoke({})["response"]


# ── /splash ──────────────────────────────────────────────────────────


class SplashState(TypedDict, total=False):
    response: dict


def _splash_node(state: SplashState) -> dict:
    _log.info("CONTROL TRANSFER: SharedServices has taken ownership of the page")
    return {"response": {
        "html": ('<div style="text-align:center;padding:20px;">'
                 '<div style="font-size:24px;font-weight:700;color:#1f2937;">Shared Services</div>'
                 '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
                 '</div>')
    }}


splash_graph = _make_single_node_graph("render_splash", _splash_node, SplashState)


@app.get("/splash")
def splash():
    return splash_graph.invoke({})["response"]


# ── /transfer/to-findcare ────────────────────────────────────────────


class TransferState(TypedDict, total=False):
    response: dict


def _transfer_to_findcare_node(state: TransferState) -> dict:
    """SharedServices releases page ownership back to FindCare."""
    _log.info("CONTROL TRANSFER: SharedServices → FindCare")
    return {"response": {"owner": "findcare", "reason": "filter_interaction"}}


transfer_to_findcare_graph = _make_single_node_graph(
    "transfer_to_findcare", _transfer_to_findcare_node, TransferState)


@app.post("/transfer/to-findcare")
def transfer_to_findcare():
    return transfer_to_findcare_graph.invoke({})["response"]


# ── /verify-token ────────────────────────────────────────────────────


class VerifyTokenRequest(_BaseModel):
    session_token: _Optional[dict] = None


class VerifyTokenState(TypedDict, total=False):
    session_token: _Optional[dict]
    response: dict


def _verify_token_node(state: VerifyTokenState) -> dict:
    """Verify a session token from FindCare. Proves mutual authentication."""
    body_token = state.get("session_token")
    token_valid = False
    token_origin = "unknown"
    if body_token:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
            os.environ.setdefault(
                "CERTS_DIR",
                os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs"))
            from session_token import verify_session_token
            token_valid = verify_session_token(body_token, "FindCare")
            token_origin = body_token.get("origin", "unknown")
            _log.info("Token verification: origin=%s valid=%s", token_origin, token_valid)
        except Exception as e:
            _log.warning("Token verification failed: %s", e)
    return {"response": {
        "status": "verified" if token_valid else "failed",
        "session_token": {
            "token_received": (body_token.get("token", "") if body_token else ""),
            "signature_received": ((body_token.get("signature", "") or "")[:40] + "...") if body_token and body_token.get("signature") else "none",
            "origin": (body_token.get("origin", "") if body_token else ""),
            "verified": token_valid,
        },
    }}


verify_token_graph = _make_single_node_graph(
    "verify_session_token", _verify_token_node, VerifyTokenState)


@app.post("/verify-token")
def verify_token(body: VerifyTokenRequest):
    return verify_token_graph.invoke({"session_token": body.session_token})["response"]


# ── /secrets/{key} ───────────────────────────────────────────────────


class GetSecretState(TypedDict, total=False):
    key: str
    response: dict


def _get_secret_node(state: GetSecretState) -> dict:
    """Stub — returns environment variable value. Production: reads from Azure
    Key Vault via x509 cert."""
    key = state.get("key", "")
    value = os.getenv(key)
    if value:
        return {"response": {"key": key, "found": True}}
    return {"response": {"key": key, "found": False, "error": "Secret not found"}}


get_secret_graph = _make_single_node_graph(
    "lookup_secret", _get_secret_node, GetSecretState)


@app.get("/secrets/{key}")
def get_secret(key: str):
    return get_secret_graph.invoke({"key": key})["response"]


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
