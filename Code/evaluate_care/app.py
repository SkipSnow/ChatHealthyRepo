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

# ── Health ──────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "evaluate_care", "version": "0.1.4"}

@app.get("/splash")
def splash():
    _log.info("CONTROL TRANSFER: EvaluateCare has taken ownership of the page")
    return {"html": '<div style="text-align:center;padding:20px;">'
            '<div style="font-size:24px;font-weight:700;color:#1f2937;">EvaluateCare</div>'
            '<div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">is still unimplemented.</div>'
            '</div>'}

@app.post("/transfer/to-findcare")
def transfer_to_findcare():
    """EvaluateCare releases page ownership back to FindCare.
    Called when user interacts with the specialty filter while in EvaluateCare mode."""
    _log.info("CONTROL TRANSFER: EvaluateCare → FindCare (user touched filter)")
    return {"owner": "findcare", "reason": "filter_interaction"}

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
