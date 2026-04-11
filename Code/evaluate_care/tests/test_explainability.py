# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Tests for EVAL-EXPLAIN — score explanations.

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from evaluate_care.models import (
    EntityType,
    MeasureInput,
    ScoringRequest,
)
from evaluate_care.scoring_engine import ScoringEngine
from evaluate_care.explainability import explain_score


def _score_provider():
    engine = ScoringEngine()
    from evaluate_care.weights import DEFAULT_PROVIDER_WEIGHTS
    measures = [
        MeasureInput(name="board_certification", value=1.0),
        MeasureInput(name="years_in_practice", value=20.0),
        MeasureInput(name="specialty_match", value=2.0),
        MeasureInput(name="prescription_behavior", value=90.0),
        MeasureInput(name="patient_ratings", value=4.0),
        MeasureInput(name="new_patient_acceptance", value=1.0),
    ]
    req = ScoringRequest(
        entity_id="prov-ex",
        entity_type=EntityType.PROVIDER,
        measures=measures,
        weights=DEFAULT_PROVIDER_WEIGHTS,
    )
    return engine.score(req, use_cache=False)


class TestExplainScore:
    def test_explanation_has_summary(self):
        resp = _score_provider()
        explanation = explain_score(resp)
        assert explanation.summary
        assert "Composite score" in explanation.summary

    def test_explanation_has_measure_details(self):
        resp = _score_provider()
        explanation = explain_score(resp)
        assert len(explanation.measure_explanations) == 6
        for me in explanation.measure_explanations:
            assert "measure" in me
            assert "normalization" in me

    def test_explanation_includes_confidence(self):
        resp = _score_provider()
        explanation = explain_score(resp)
        assert explanation.confidence is not None
        assert "Confidence" in explanation.summary

    def test_error_explanation(self):
        from evaluate_care.models import ScoringResponse
        resp = ScoringResponse(
            entity_id="err-1",
            entity_type=EntityType.PROVIDER,
            error="All measures failed",
            error_code="EVAL_ALL_FAILED",
        )
        explanation = explain_score(resp)
        assert "Scoring failed" in explanation.summary

    def test_missing_measures_noted(self):
        engine = ScoringEngine()
        from evaluate_care.weights import DEFAULT_PROVIDER_WEIGHTS
        measures = [
            MeasureInput(name="board_certification", value=1.0),
            MeasureInput(name="years_in_practice", value=None, missing=True),
            MeasureInput(name="specialty_match", value=2.0),
            MeasureInput(name="prescription_behavior", value=None, missing=True),
            MeasureInput(name="patient_ratings", value=4.0),
            MeasureInput(name="new_patient_acceptance", value=1.0),
        ]
        req = ScoringRequest(
            entity_id="prov-miss",
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=DEFAULT_PROVIDER_WEIGHTS,
        )
        resp = engine.score(req, use_cache=False)
        explanation = explain_score(resp)
        assert "Missing data" in explanation.summary
        missing = [me for me in explanation.measure_explanations if me.get("missing")]
        assert len(missing) == 2
        for me in missing:
            assert "note" in me
