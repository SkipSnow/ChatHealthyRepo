# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Tests for the composite scoring engine (EVAL-SCORE-PROV, EVAL-SCORE-CT).

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from evaluate_care.models import (
    EntityType,
    MeasureInput,
    ScoringRequest,
    WeightConfig,
    WeightEntry,
)
from evaluate_care.scoring_engine import ScoringEngine


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _provider_request(
    board_cert=1.0,
    years=15.0,
    specialty=2.0,
    rx=80.0,
    ratings=4.5,
    new_patients=1.0,
) -> ScoringRequest:
    measures = [
        MeasureInput(name="board_certification", value=board_cert),
        MeasureInput(name="years_in_practice", value=years),
        MeasureInput(name="specialty_match", value=specialty),
        MeasureInput(name="prescription_behavior", value=rx),
        MeasureInput(name="patient_ratings", value=ratings),
        MeasureInput(name="new_patient_acceptance", value=new_patients),
    ]
    from evaluate_care.weights import DEFAULT_PROVIDER_WEIGHTS
    return ScoringRequest(
        entity_id="prov-001",
        entity_type=EntityType.PROVIDER,
        measures=measures,
        weights=DEFAULT_PROVIDER_WEIGHTS,
    )


def _trial_request() -> ScoringRequest:
    measures = [
        MeasureInput(name="trial_phase", value=3.0),
        MeasureInput(name="trial_recruitment", value=3.0),
        MeasureInput(name="trial_condition_relevance", value=0.9),
        MeasureInput(name="trial_sponsor", value=3.0),
        MeasureInput(name="trial_proximity", value=10.0),
        MeasureInput(name="trial_recency", value=30.0),
        MeasureInput(name="trial_eligibility", value=85.0),
    ]
    from evaluate_care.weights import DEFAULT_CLINICAL_TRIAL_WEIGHTS
    return ScoringRequest(
        entity_id="trial-001",
        entity_type=EntityType.CLINICAL_TRIAL,
        measures=measures,
        weights=DEFAULT_CLINICAL_TRIAL_WEIGHTS,
    )


# ---------------------------------------------------------------------------
# Determinism (EPIC-001-F-020-S-004-REQ-T-001, EPIC-001-F-020-S-004-REQ-T-002)
# ---------------------------------------------------------------------------

class TestDeterminism:
    def test_identical_inputs_produce_identical_outputs(self):
        engine = ScoringEngine()
        r1 = engine.score(_provider_request(), use_cache=False)
        r2 = engine.score(_provider_request(), use_cache=False)
        assert r1.composite_score == r2.composite_score
        assert len(r1.measures) == len(r2.measures)
        for m1, m2 in zip(r1.measures, r2.measures):
            assert m1.normalized_score == m2.normalized_score
            assert m1.weighted_contribution == m2.weighted_contribution

    def test_repeated_runs_same_score(self):
        engine = ScoringEngine()
        scores = [
            engine.score(_provider_request(), use_cache=False).composite_score
            for _ in range(10)
        ]
        assert len(set(scores)) == 1


# ---------------------------------------------------------------------------
# Composite score correctness
# ---------------------------------------------------------------------------

class TestProviderScoring:
    def test_perfect_provider_scores_high(self):
        engine = ScoringEngine()
        req = _provider_request(
            board_cert=1.0, years=30.0, specialty=2.0,
            rx=100.0, ratings=5.0, new_patients=1.0,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.composite_score is not None
        assert resp.composite_score == 1.0
        assert resp.error is None

    def test_zero_provider_scores_zero(self):
        engine = ScoringEngine()
        req = _provider_request(
            board_cert=0.0, years=0.0, specialty=0.0,
            rx=0.0, ratings=0.0, new_patients=0.0,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.composite_score == 0.0

    def test_composite_between_zero_and_one(self):
        engine = ScoringEngine()
        resp = engine.score(_provider_request(), use_cache=False)
        assert 0.0 <= resp.composite_score <= 1.0

    def test_subscores_present(self):
        engine = ScoringEngine()
        resp = engine.score(_provider_request(), use_cache=False)
        assert len(resp.measures) == 6
        for m in resp.measures:
            assert m.normalized_score is not None
            assert m.weight > 0

    def test_trace_present(self):
        engine = ScoringEngine()
        resp = engine.score(_provider_request(), use_cache=False)
        assert len(resp.trace) == 6
        for t in resp.trace:
            assert t.measure_name
            assert t.weight_applied > 0


class TestClinicalTrialScoring:
    def test_trial_scores_high_quality(self):
        engine = ScoringEngine()
        resp = engine.score(_trial_request(), use_cache=False)
        assert resp.composite_score is not None
        assert resp.composite_score > 0.7
        assert resp.error is None

    def test_trial_subscores(self):
        engine = ScoringEngine()
        resp = engine.score(_trial_request(), use_cache=False)
        assert len(resp.measures) == 7

    def test_convenience_method(self):
        engine = ScoringEngine()
        measures = [
            MeasureInput(name="trial_phase", value=3.0),
            MeasureInput(name="trial_recruitment", value=3.0),
            MeasureInput(name="trial_condition_relevance", value=0.9),
            MeasureInput(name="trial_sponsor", value=3.0),
            MeasureInput(name="trial_proximity", value=10.0),
            MeasureInput(name="trial_recency", value=30.0),
            MeasureInput(name="trial_eligibility", value=85.0),
        ]
        resp = engine.score_clinical_trial("t-1", measures, use_cache=False)
        assert resp.composite_score is not None
        assert resp.entity_type == EntityType.CLINICAL_TRIAL


# ---------------------------------------------------------------------------
# Missing data handling (EVAL-FAILSAFE)
# ---------------------------------------------------------------------------

class TestFailsafe:
    def test_all_missing_returns_error(self):
        engine = ScoringEngine()
        measures = [
            MeasureInput(name="board_certification", value=None, missing=True),
            MeasureInput(name="years_in_practice", value=None, missing=True),
        ]
        from evaluate_care.weights import DEFAULT_PROVIDER_WEIGHTS
        req = ScoringRequest(
            entity_id="prov-missing",
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=DEFAULT_PROVIDER_WEIGHTS,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.composite_score is None
        assert resp.error is not None
        assert resp.error_code == "EVAL_NO_DATA"

    def test_partial_missing_still_scores(self):
        engine = ScoringEngine()
        measures = [
            MeasureInput(name="board_certification", value=1.0),
            MeasureInput(name="years_in_practice", value=None, missing=True),
            MeasureInput(name="specialty_match", value=2.0),
            MeasureInput(name="prescription_behavior", value=None, missing=True),
            MeasureInput(name="patient_ratings", value=4.0),
            MeasureInput(name="new_patient_acceptance", value=1.0),
        ]
        from evaluate_care.weights import DEFAULT_PROVIDER_WEIGHTS
        req = ScoringRequest(
            entity_id="prov-partial",
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=DEFAULT_PROVIDER_WEIGHTS,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.composite_score is not None
        assert 0.0 <= resp.composite_score <= 1.0
        missing_measures = [m for m in resp.measures if m.missing_data]
        assert len(missing_measures) == 2

    def test_single_measure_scores(self):
        engine = ScoringEngine()
        measures = [
            MeasureInput(name="board_certification", value=1.0),
        ]
        weights = WeightConfig(weights=[
            WeightEntry(measure_name="board_certification", weight=1.0),
        ])
        req = ScoringRequest(
            entity_id="prov-single",
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=weights,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.composite_score == 1.0


# ---------------------------------------------------------------------------
# Weight validation (EVAL-WEIGHTS)
# ---------------------------------------------------------------------------

class TestWeightValidation:
    def test_zero_weight_sum_errors(self):
        engine = ScoringEngine()
        measures = [
            MeasureInput(name="board_certification", value=1.0),
        ]
        weights = WeightConfig(weights=[
            WeightEntry(measure_name="board_certification", weight=0.0),
        ])
        req = ScoringRequest(
            entity_id="prov-zw",
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=weights,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.error is not None
        assert resp.error_code == "EVAL_ZERO_WEIGHTS"

    def test_negative_weight_rejected(self):
        with pytest.raises(Exception):
            WeightEntry(measure_name="test", weight=-1.0)

    def test_missing_weight_rejected(self):
        measures = [
            MeasureInput(name="board_certification", value=1.0),
        ]
        weights = WeightConfig(weights=[
            WeightEntry(measure_name="other_measure", weight=1.0),
        ])
        with pytest.raises(ValueError, match="no corresponding weight"):
            ScoringRequest(
                entity_id="prov-mw",
                entity_type=EntityType.PROVIDER,
                measures=measures,
                weights=weights,
            )


# ---------------------------------------------------------------------------
# Provenance (EVAL-DATA-PROVENANCE)
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_provenance_attached(self):
        engine = ScoringEngine()
        resp = engine.score(_provider_request(), use_cache=False)
        assert resp.provenance is not None
        assert resp.provenance.engine_version == "0.1.4"
        assert resp.provenance.input_hash is not None
        assert len(resp.provenance.measure_sources) > 0

    def test_provenance_hash_deterministic(self):
        engine = ScoringEngine()
        r1 = engine.score(_provider_request(), use_cache=False)
        r2 = engine.score(_provider_request(), use_cache=False)
        assert r1.provenance.input_hash == r2.provenance.input_hash


# ---------------------------------------------------------------------------
# Confidence (EVAL-CONFIDENCE)
# ---------------------------------------------------------------------------

class TestConfidence:
    def test_full_data_high_confidence(self):
        engine = ScoringEngine()
        resp = engine.score(_provider_request(), use_cache=False)
        assert resp.confidence is not None
        assert resp.confidence.level.value == "high"
        assert resp.confidence.completeness_ratio == 1.0
        assert resp.confidence.missing_measures == []

    def test_partial_data_lower_confidence(self):
        engine = ScoringEngine()
        measures = [
            MeasureInput(name="board_certification", value=1.0),
            MeasureInput(name="years_in_practice", value=None, missing=True),
            MeasureInput(name="specialty_match", value=None, missing=True),
            MeasureInput(name="prescription_behavior", value=None, missing=True),
            MeasureInput(name="patient_ratings", value=None, missing=True),
            MeasureInput(name="new_patient_acceptance", value=1.0),
        ]
        from evaluate_care.weights import DEFAULT_PROVIDER_WEIGHTS
        req = ScoringRequest(
            entity_id="prov-low",
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=DEFAULT_PROVIDER_WEIGHTS,
        )
        resp = engine.score(req, use_cache=False)
        assert resp.confidence.level.value in ("low", "medium")
        assert len(resp.confidence.missing_measures) == 4


# ---------------------------------------------------------------------------
# Input immutability (EPIC-001-F-020-S-004-REQ-T-010)
# ---------------------------------------------------------------------------

class TestImmutability:
    def test_input_not_mutated(self):
        req = _provider_request()
        original_measures = [m.model_dump() for m in req.measures]
        engine = ScoringEngine()
        engine.score(req, use_cache=False)
        for orig, current in zip(original_measures, req.measures):
            assert orig["value"] == current.value
            assert orig["name"] == current.name
