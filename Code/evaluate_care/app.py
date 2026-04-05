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

from models import (
    ProviderQualityInput, ProviderQualityOutput,
    ClinicalTrialQualityInput, ClinicalTrialQualityOutput,
    ExplanationOutput,
)
from scoring_engine import ScoringEngine

logging.basicConfig(level=logging.INFO)
_log = logging.getLogger("evaluate_care")

app = FastAPI(title="ChatHealthy.ai EvaluateCare", version="0.1.4")

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
    measures: dict = Field(..., description="Provider measure values keyed by measure name")
    weights: dict = Field(default_factory=dict, description="Optional weight overrides")

@app.post("/score/provider")
def score_provider(body: ScoreProviderRequest):
    result = _engine.score_provider(body.measures, body.weights or None)
    return result

# ── Clinical Trial Scoring ──────────────────────────────────

class ScoreTrialRequest(BaseModel):
    measures: dict = Field(..., description="Trial measure values keyed by measure name")
    weights: dict = Field(default_factory=dict, description="Optional weight overrides")

@app.post("/score/trial")
def score_trial(body: ScoreTrialRequest):
    result = _engine.score_trial(body.measures, body.weights or None)
    return result

# ── Explanation ─────────────────────────────────────────────

class ExplainRequest(BaseModel):
    score_output: dict = Field(..., description="Output from /score/provider or /score/trial")

@app.post("/explain")
def explain(body: ExplainRequest):
    from explainability import explain_score
    return explain_score(body.score_output)

# ── Run ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8001"))
    _log.info("EvaluateCare starting on port %d", port)
    uvicorn.run(app, host="0.0.0.0", port=port)
