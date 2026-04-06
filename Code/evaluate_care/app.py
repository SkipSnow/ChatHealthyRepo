# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EvaluateCare Service — FastAPI app on port 8001.
# Separate service from FindCare (GOV-005, EPIC-6).

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
    allow_origins=["http://localhost", "http://localhost:80", "http://localhost:5173",
                   "http://localhost:8000", "https://chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"http://localhost(:\d+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_engine = ScoringEngine()

# ── Health ──────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "evaluate_care", "version": "0.1.4"}

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
    question_summary: str = Field(default="", description="Why the user needs evaluation")

@app.post("/evaluate/providers")
def evaluate_providers(body: EvaluateProvidersRequest):
    """Stub — accepts provider list from FindCare facade and returns
    provider names, specialty, and NPI for display. No scoring logic yet."""
    results = []
    for p in body.providers:
        results.append({
            "name": p.get("name", "Unknown"),
            "specialty": p.get("specialty", p.get("primary_specialty", "Unknown")),
            "npi": p.get("npi", "Unknown"),
        })
    return {
        "status": "stub",
        "evaluated_providers": results,
        "question_summary": body.question_summary or "Provider evaluation requested",
        "note": "Scoring engine not yet implemented — displaying provider identity only",
    }

# ── Run ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    _log.info("EvaluateCare starting on port %d", port)
    uvicorn.run("evaluate_care.app:app", host="0.0.0.0", port=port)
