# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EvaluateCare Service — FastAPI app on port 8001.
# Separate service from FindCare (GOV-005).
#
# v4-043: every route handler routes business logic through a LangGraph
# StateGraph (compiled once at module load, invoked from the handler body).
# Business-logic graphs live in evaluate_care/graphs/. Mechanical handler
# graphs (health, splash, etc.) are defined inline below.

import os
import sys
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END

# Add evaluate_care to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from evaluate_care.models import ScoringRequest, ScoringResponse
from evaluate_care.scoring_engine import ScoringEngine

import time
from fastapi import Request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("evaluate_care")


def _bootstrap_certs_from_env():
    """SEC-HTTPS-001-REQ-016: decode PEM certs from HF Space Secrets into a
    runtime directory so session_token.verify_session_token (which needs
    findcare.crt) can find them on HF. No-op locally where /certs is bind-
    mounted and CERTS_DIR is already set."""
    import base64
    runtime_dir = "/tmp/ch_certs"
    mapping = {
        "FINDCARE_CERT_PEM": "findcare.crt",
        "EVALCARE_CERT_PEM": "evalcare.crt",
        "EVALCARE_SIGNING_KEY_PEM": "evalcare.key",
        "CA_CERT_PEM":       "ca.crt",
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

app = FastAPI(title="ChatHealthy.ai EvaluateCare", version="0.1.4")

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
    allow_origins=["https://localhost", "https://localhost:443", "https://localhost:7860",
                   "https://localhost:8001", "https://localhost:8002",
                   "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_engine = ScoringEngine()
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
        _log.warning("evaluate_care /health: MongoClient init failed: %s", e)
        return None


# ── Business-logic LangGraph orchestrators (built in evaluate_care/graphs/) ──

from evaluate_care.explainability import explain_score as _explain_score
from evaluate_care.graphs import (
    build_score_provider_graph,
    build_score_trial_graph,
    build_explain_graph,
    build_evaluate_providers_graph,
    build_evaluate_view_graph,
)

_last_evaluation = {"providers": [], "question": ""}

score_provider_graph = build_score_provider_graph({"engine": _engine})
score_trial_graph = build_score_trial_graph({"engine": _engine})
explain_graph = build_explain_graph({"explain_fn": _explain_score})
evaluate_providers_graph = build_evaluate_providers_graph({"last_evaluation": _last_evaluation})
evaluate_view_graph = build_evaluate_view_graph({"last_evaluation": _last_evaluation})


# ── Mechanical handler graphs (inline single-node StateGraphs) ──


def _make_single_node_graph(node_name, fn, state_class):
    g = StateGraph(state_class)
    g.add_node(node_name, fn)
    g.add_edge(START, node_name)
    g.add_edge(node_name, END)
    return g.compile()


# ── /debug/verify-live ───────────────────────────────────────


class DebugVerifyLiveState(TypedDict, total=False):
    body: dict
    response: dict


def _debug_verify_live_node(state: DebugVerifyLiveState) -> dict:
    if os.getenv("DEBUG", "false").lower() not in ("1", "true", "yes"):
        return {"response": {"disabled": "DEBUG not set"}}
    import traceback
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    body = state.get("body", {})
    result = {"verified": None, "error": None, "details": {}}
    try:
        from session_token import verify_session_token, CERTS_DIR as MODULE_CERTS_DIR
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
        result["details"]["token_signed"] = body.get("signed")
        result["details"]["token_origin"] = body.get("origin")
        result["details"]["token_len"] = len(body.get("token", ""))
        result["details"]["sig_len"] = len(body.get("signature", ""))
        result["verified"] = verify_session_token(body, "FindCare")
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
        result["tb"] = traceback.format_exc()
    return {"response": result}


debug_verify_live_graph = _make_single_node_graph(
    "debug_verify_live", _debug_verify_live_node, DebugVerifyLiveState)


@app.post("/debug/verify-live")
def debug_verify_live(body: dict):
    return debug_verify_live_graph.invoke({"body": body})["response"]


# ── /debug/bootstrap ─────────────────────────────────────────


class DebugBootstrapState(TypedDict, total=False):
    response: dict


def _debug_bootstrap_node(state: DebugBootstrapState) -> dict:
    if os.getenv("DEBUG", "false").lower() not in ("1", "true", "yes"):
        return {"response": {"disabled": "set DEBUG=1 on the Space to enable this endpoint"}}
    certs_dir = os.environ.get("CERTS_DIR", "<unset>")
    files = []
    if os.path.isdir(certs_dir):
        for f in sorted(os.listdir(certs_dir)):
            p = os.path.join(certs_dir, f)
            files.append({"name": f, "size": os.path.getsize(p)})
    return {"response": {
        "CERTS_DIR": certs_dir,
        "certs_dir_files": files,
        "env_present": {
            "FINDCARE_CERT_PEM":        bool(os.environ.get("FINDCARE_CERT_PEM")),
            "EVALCARE_CERT_PEM":        bool(os.environ.get("EVALCARE_CERT_PEM")),
            "EVALCARE_SIGNING_KEY_PEM": bool(os.environ.get("EVALCARE_SIGNING_KEY_PEM")),
            "CA_CERT_PEM":              bool(os.environ.get("CA_CERT_PEM")),
        },
    }}


debug_bootstrap_graph = _make_single_node_graph(
    "debug_bootstrap", _debug_bootstrap_node, DebugBootstrapState)


@app.get("/debug/bootstrap")
def debug_bootstrap():
    return debug_bootstrap_graph.invoke({})["response"]


# ── /health ──────────────────────────────────────────────────


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
            _log.warning("evaluate_care /health: MongoDB read failed: %s", e)
    return {"response": {
        "status": "ok", "service": "evaluate_care",
        "db": db_status, "env": _ENV_PREFIX,
        "build": _build, "version": _version_str, "framework": _framework_str,
    }}


health_graph = _make_single_node_graph("health_check", _health_node, HealthState)


@app.get("/health")
def health():
    return health_graph.invoke({})["response"]


# ── /splash ──────────────────────────────────────────────────


class SplashState(TypedDict, total=False):
    response: dict


def _splash_node(state: SplashState) -> dict:
    _log.info("CONTROL TRANSFER: EvaluateCare has taken ownership of the page")
    return {"response": {
        "html": ('<div style="text-align:center;padding:20px;">'
                 '<div style="font-size:24px;font-weight:700;color:#1f2937;">EvaluateCare</div>'
                 '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
                 '</div>')
    }}


splash_graph = _make_single_node_graph("render_splash", _splash_node, SplashState)


@app.get("/splash")
def splash():
    return splash_graph.invoke({})["response"]


# ── /transfer/to-findcare ────────────────────────────────────


class TransferState(TypedDict, total=False):
    response: dict


def _transfer_to_findcare_node(state: TransferState) -> dict:
    """EvaluateCare releases page ownership back to FindCare.
    Called when user interacts with the specialty filter while in EvaluateCare mode."""
    _log.info("CONTROL TRANSFER: EvaluateCare → FindCare (user touched filter)")
    return {"response": {"owner": "findcare", "reason": "filter_interaction"}}


transfer_to_findcare_graph = _make_single_node_graph(
    "transfer_to_findcare", _transfer_to_findcare_node, TransferState)


@app.post("/transfer/to-findcare")
def transfer_to_findcare():
    return transfer_to_findcare_graph.invoke({})["response"]


# ── /verify-token ────────────────────────────────────────────


from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional


class _VerifyTokenRequest(_BaseModel):
    session_token: _Optional[dict] = None


class VerifyTokenState(TypedDict, total=False):
    session_token: _Optional[dict]
    response: dict


def _verify_token_node(state: VerifyTokenState) -> dict:
    """Verify a session token from FindCare. Proves mutual authentication."""
    body_token = state.get("session_token")
    token_valid = False
    if body_token:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
            os.environ.setdefault(
                "CERTS_DIR",
                os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs"))
            from session_token import verify_session_token
            token_valid = verify_session_token(body_token, "FindCare")
            _log.info("EvalCare token verification: origin=%s valid=%s",
                      body_token.get("origin", "unknown"), token_valid)
        except Exception as e:
            _log.warning("EvalCare token verification failed: %s", e)
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
def verify_token(body: _VerifyTokenRequest):
    return verify_token_graph.invoke({"session_token": body.session_token})["response"]


# ── Provider Scoring ────────────────────────────────────────


class ScoreProviderRequest(BaseModel):
    provider_id: str = Field(..., description="Provider NPI or ID")
    measures: list[dict] = Field(..., description="List of {name, value} measure inputs")


@app.post("/score/provider")
def score_provider(body: ScoreProviderRequest):
    return score_provider_graph.invoke({"request": body.model_dump()})["response"]


# ── Clinical Trial Scoring ──────────────────────────────────


class ScoreTrialRequest(BaseModel):
    trial_id: str = Field(..., description="Clinical trial NCT ID")
    measures: list[dict] = Field(..., description="List of {name, value} measure inputs")


@app.post("/score/trial")
def score_trial(body: ScoreTrialRequest):
    return score_trial_graph.invoke({"request": body.model_dump()})["response"]


# ── Explanation ─────────────────────────────────────────────


class ExplainRequest(BaseModel):
    score_output: dict = Field(..., description="Output from /score/provider or /score/trial")


@app.post("/explain")
def explain(body: ExplainRequest):
    return explain_graph.invoke({"request": body.model_dump()})["response"]


# ── Evaluate Providers (FindCare handoff) ──────────


class EvaluateProvidersRequest(BaseModel):
    providers: list[dict] = Field(..., description="List of provider records from FindCare")
    chat_history: list[dict] = Field(default=[], description="Full chat history")
    session_token: dict | None = Field(default=None, description="Signed session token from FindCare")
    question_summary: str = Field(default="", description="Why the user needs evaluation")


@app.post("/evaluate/providers")
def evaluate_providers(body: EvaluateProvidersRequest):
    """Accepts provider list from FindCare and displays them."""
    return evaluate_providers_graph.invoke({"request": body.model_dump()})["response"]


@app.get("/evaluate/view")
def evaluate_view():
    """HTML page showing the last evaluation received from FindCare."""
    from fastapi.responses import HTMLResponse
    result = evaluate_view_graph.invoke({})["response"]
    return HTMLResponse(content=result["html"])


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
        # mTLS enforcement deferred per BUG-SEC-002 (browser-facing port).
    uvicorn.run(app, **kwargs)
