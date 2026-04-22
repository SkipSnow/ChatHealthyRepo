# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EvaluateCare Service — FastAPI app on port 8001.
# Separate service from FindCare (GOV-005).

import os
import sys
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

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
    allow_origins=["https://localhost", "https://localhost:443", "https://localhost:3000",
                   "https://localhost:8080", "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_engine = ScoringEngine()

from evaluate_care.explainability import explain_score as _explain_score

_last_evaluation = {"providers": [], "question": ""}

# ── Debug back door (enabled only when DEBUG=1) ─────────────
# graph-exempt: debug endpoint, no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.post("/debug/verify-live")
def debug_verify_live(body: dict):
    """Runs verify_session_token on a posted token and returns structured
    diagnostic. Gated by DEBUG=1."""
    if os.getenv("DEBUG", "false").lower() not in ("1", "true", "yes"):
        return {"disabled": "DEBUG not set"}
    import traceback
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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
    return result


# graph-exempt: debug endpoint, no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.get("/debug/bootstrap")
def debug_bootstrap():
    """Back door for mTLS/cert audit. Gated by DEBUG=1 env var so prod
    is unaffected. Reports whether the HF Space Secret bootstrap actually
    wrote findcare.crt + peer certs into CERTS_DIR."""
    if os.getenv("DEBUG", "false").lower() not in ("1", "true", "yes"):
        return {"disabled": "set DEBUG=1 on the Space to enable this endpoint"}
    certs_dir = os.environ.get("CERTS_DIR", "<unset>")
    files = []
    if os.path.isdir(certs_dir):
        for f in sorted(os.listdir(certs_dir)):
            p = os.path.join(certs_dir, f)
            files.append({"name": f, "size": os.path.getsize(p)})
    return {
        "CERTS_DIR": certs_dir,
        "certs_dir_files": files,
        "env_present": {
            "FINDCARE_CERT_PEM":        bool(os.environ.get("FINDCARE_CERT_PEM")),
            "EVALCARE_CERT_PEM":        bool(os.environ.get("EVALCARE_CERT_PEM")),
            "EVALCARE_SIGNING_KEY_PEM": bool(os.environ.get("EVALCARE_SIGNING_KEY_PEM")),
            "CA_CERT_PEM":              bool(os.environ.get("CA_CERT_PEM")),
        },
    }


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
        _log.warning("evaluate_care /health: MongoClient init failed: %s", e)
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
            _log.warning("evaluate_care /health: MongoDB read failed: %s", e)
    return {"status": "ok", "service": "evaluate_care",
            "db": db_status, "env": _ENV_PREFIX,
            "build": _build, "version": _version_str, "framework": _framework_str}

# graph-exempt: static page render — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.get("/splash")
def splash():
    _log.info("CONTROL TRANSFER: EvaluateCare has taken ownership of the page")
    return {"html": '<div style="text-align:center;padding:20px;">'
            '<div style="font-size:24px;font-weight:700;color:#1f2937;">EvaluateCare</div>'
            '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
            '</div>'}

# graph-exempt: proxy/redirect — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.post("/transfer/to-findcare")
def transfer_to_findcare():
    """EvaluateCare releases page ownership back to FindCare.
    Called when user interacts with the specialty filter while in EvaluateCare mode."""
    _log.info("CONTROL TRANSFER: EvaluateCare → FindCare (user touched filter)")
    return {"owner": "findcare", "reason": "filter_interaction"}

# ── Session Token — minted by EvaluateCare ─────────────────
# SEC-HTTPS-001-REQ-021: when EvaluateCare owns the page, the right-panel
# token MUST be minted by EvaluateCare (not relayed from FindCare). The
# token is signed with evalcare.key; FindCare verifies with evalcare.crt.
# graph-exempt: session token issuance, no LLM — security primitive; per BUG-ARCH-GRAPH-EXEMPT-001
@app.get("/session")
def get_session():
    """Generate an EvaluateCare-signed session token.

    NO FALLBACK. If evalcare.key is missing or generation fails, propagate
    so the caller halts rather than running with a placeholder.
    See feedback_no_security_fallbacks.
    """
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
    os.environ.setdefault("CERTS_DIR", os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs"))
    from session_token import generate_session_token
    token = generate_session_token("EvaluateCare")
    # SEC-HTTPS-001-REQ-020: server-asserted env from this container's _ENV_PREFIX.
    token["server_env"] = _ENV_PREFIX
    _log.info("Minted EvaluateCare session token: env=%s nonce_len=%d",
              _ENV_PREFIX, len(token.get("token", "")))
    return token

# ── Token Verification (DEVOPS-BANNER-B006 / SEC-HTTPS-001-REQ-013) ─────
from pydantic import BaseModel as _BaseModel
from typing import Optional as _Optional

class _VerifyTokenRequest(_BaseModel):
    session_token: _Optional[dict] = None

# graph-exempt: mTLS/session security primitive, no LLM; per BUG-ARCH-GRAPH-EXEMPT-001
@app.post("/verify-token")
def verify_token(body: _VerifyTokenRequest):
    """Verify a session token from FindCare. Proves mutual authentication.
    Mirrors SharedServices /verify-token pattern."""
    token_valid = False
    if body.session_token:
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
            os.environ.setdefault("CERTS_DIR", os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs"))
            from session_token import verify_session_token
            token_valid = verify_session_token(body.session_token, "FindCare")
            _log.info("EvalCare token verification: origin=%s valid=%s",
                      body.session_token.get("origin", "unknown"), token_valid)
        except Exception as e:
            _log.warning("EvalCare token verification failed: %s", e)
    return {
        "status": "verified" if token_valid else "failed",
        "session_token": {
            "token_received": body.session_token.get("token", "") if body.session_token else "",
            "signature_received": (body.session_token.get("signature", "") or "")[:40] + "..." if body.session_token and body.session_token.get("signature") else "none",
            # SEC-HTTPS-001-REQ-020: origin field is the name of the
            # RESPONDING service (self-identification). Distinct from
            # the cryptographic signer (REQ-017, always FindCare).
            "origin": "EvaluateCare",
            "verified": token_valid,
        },
    }

# ── Provider Scoring ────────────────────────────────────────

class ScoreProviderRequest(BaseModel):
    provider_id: str = Field(..., description="Provider NPI or ID")
    measures: list[dict] = Field(..., description="List of {name, value} measure inputs")

@app.post("/score/provider")
def score_provider(body: ScoreProviderRequest):
    from evaluate_care.models import MeasureInput
    measure_inputs = []
    for m in body.measures:
        # Auto-route: numeric → value, non-numeric → raw_value
        val = m.get("value")
        if isinstance(val, (int, float, bool)):
            measure_inputs.append(MeasureInput(name=m["name"], value=float(val) if not isinstance(val, bool) else (1.0 if val else 0.0), raw_value=val))
        else:
            measure_inputs.append(MeasureInput(name=m["name"], raw_value=val))
    result = _engine.score_provider(body.provider_id, measure_inputs)
    return result.model_dump()

# ── Clinical Trial Scoring ──────────────────────────────────

class ScoreTrialRequest(BaseModel):
    trial_id: str = Field(..., description="Clinical trial NCT ID")
    measures: list[dict] = Field(..., description="List of {name, value} measure inputs")

@app.post("/score/trial")
def score_trial(body: ScoreTrialRequest):
    from evaluate_care.models import MeasureInput
    measure_inputs = []
    for m in body.measures:
        val = m.get("value")
        if isinstance(val, (int, float, bool)):
            measure_inputs.append(MeasureInput(name=m["name"], value=float(val) if not isinstance(val, bool) else (1.0 if val else 0.0), raw_value=val))
        else:
            measure_inputs.append(MeasureInput(name=m["name"], raw_value=val))
    result = _engine.score_clinical_trial(body.trial_id, measure_inputs)
    return result.model_dump()

# ── Explanation ─────────────────────────────────────────────

class ExplainRequest(BaseModel):
    score_output: dict = Field(..., description="Output from /score/provider or /score/trial")

@app.post("/explain")
def explain(body: ExplainRequest):
    from evaluate_care.explainability import explain_score
    return explain_score(body.score_output)

# ── Evaluate Providers (stub — FindCare handoff) ──────────

class EvaluateProvidersRequest(BaseModel):
    providers: list[dict] = Field(..., description="List of provider records from FindCare")
    chat_history: list[dict] = Field(default=[], description="Full chat history")
    session_token: dict | None = Field(default=None, description="Signed session token from FindCare")
    question_summary: str = Field(default="", description="Why the user needs evaluation")

_last_evaluation = {"providers": [], "question": ""}

@app.post("/evaluate/providers")
def evaluate_providers(body: EvaluateProvidersRequest):
    """Accepts provider list from FindCare and displays them."""
    # Verify session token
    token_valid = False
    token_origin = "unknown"
    if body.session_token:
        try:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "Shared"))
            os.environ.setdefault("CERTS_DIR", os.path.join(os.path.dirname(__file__), "..", "Shared", "ops", "certs"))
            from session_token import verify_session_token
            token_valid = verify_session_token(body.session_token, "FindCare")
            token_origin = body.session_token.get("origin", "unknown")
            _log.info("Session token: origin=%s valid=%s token=%s",
                      token_origin, token_valid, body.session_token.get("token", "?")[:20])
        except Exception as e:
            _log.warning("Session token verification failed: %s", e)

    results = []
    for p in body.providers:
        results.append({
            "name": p.get("name", "Unknown"),
            "specialty": p.get("specialty", p.get("primary_specialty", "Unknown")),
            "npi": p.get("npi", "Unknown"),
        })
    _last_evaluation["providers"] = results
    _last_evaluation["question"] = body.question_summary or "Provider evaluation"
    _log.info("Received %d providers from FindCare (token_valid=%s): %s",
              len(results), token_valid, _last_evaluation["question"])
    return {
        "status": "received",
        "evaluated_providers": results,
        "question_summary": _last_evaluation["question"],
        "session_token": {
            "token_received": body.session_token.get("token", "") if body.session_token else "",
            "signature_received": (body.session_token.get("signature", "") or "")[:40] + "..." if body.session_token and body.session_token.get("signature") else "none",
            "origin": body.session_token.get("origin", "") if body.session_token else "",
            "verified": token_valid,
        },
    }

@app.get("/evaluate/view")
def evaluate_view():
    """HTML page showing the last evaluation received from FindCare."""
    from fastapi.responses import HTMLResponse
    providers = _last_evaluation.get("providers", [])
    question = _last_evaluation.get("question", "No evaluation received yet")
    rows = ""
    for i, p in enumerate(providers):
        rows += f"<tr><td>{i+1}</td><td>{p['name']}</td><td>{p['specialty']}</td><td>{p['npi']}</td></tr>"
    if not rows:
        rows = "<tr><td colspan='4' style='text-align:center;color:#999;'>No providers received yet. Click 'Evaluate These Providers' in FindCare.</td></tr>"
    html = f"""<!DOCTYPE html>
<html><head><title>EvaluateCare — Provider Evaluation</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 40px; background: #f8fffe; }}
h1 {{ color: #0b7a75; }}
h2 {{ color: #374151; font-size: 16px; }}
table {{ border-collapse: collapse; width: 100%; margin-top: 16px; }}
th {{ background: #0b7a75; color: white; padding: 10px; text-align: left; }}
td {{ padding: 8px 10px; border-bottom: 1px solid #e5e7eb; }}
tr:hover {{ background: #f0fffe; }}
.badge {{ background: #d1fae5; color: #065f46; padding: 2px 8px; border-radius: 4px; font-size: 12px; }}
.header {{ display: flex; justify-content: space-between; align-items: center; }}
</style></head><body>
<div class="header">
    <h1>EvaluateCare — Provider Evaluation</h1>
    <span class="badge">Service: localhost:8001 (separate from FindCare)</span>
</div>
<h2>Query: {question}</h2>
<p>{len(providers)} providers received from FindCare via handoff</p>
<table>
<tr><th>#</th><th>Provider</th><th>Specialty</th><th>NPI</th></tr>
{rows}
</table>
<p style="color:#999;font-size:12px;margin-top:24px;">
    This page proves the FindCare → EvaluateCare handoff. Providers were sent from FindCare (:8000) to EvaluateCare (:8001) as a separate service.
    In production, this communication uses mTLS with x509 certificates.
</p>
</body></html>"""
    return HTMLResponse(content=html)

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
