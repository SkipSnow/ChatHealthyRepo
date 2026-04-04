# Copyright (c) 2026 Skip Snow. All rights reserved.
# Tests for individual measures (EVAL-P-MEASURE-*, EVAL-CT-MEASURE-*, EVAL-P-RX).

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from evaluate_care.models import MeasureInput
from evaluate_care.measures.board_certification import BoardCertificationMeasure
from evaluate_care.measures.years_in_practice import YearsInPracticeMeasure
from evaluate_care.measures.specialty_match import SpecialtyMatchMeasure
from evaluate_care.measures.prescription_behavior import PrescriptionBehaviorMeasure
from evaluate_care.measures.patient_ratings import PatientRatingsMeasure
from evaluate_care.measures.new_patient_acceptance import NewPatientAcceptanceMeasure
from evaluate_care.measures.trial_phase import TrialPhaseMeasure
from evaluate_care.measures.trial_recruitment import TrialRecruitmentMeasure
from evaluate_care.measures.trial_condition_relevance import TrialConditionRelevanceMeasure
from evaluate_care.measures.trial_sponsor import TrialSponsorMeasure
from evaluate_care.measures.trial_proximity import TrialProximityMeasure
from evaluate_care.measures.trial_recency import TrialRecencyMeasure
from evaluate_care.measures.trial_eligibility import TrialEligibilityMeasure


# ---------------------------------------------------------------------------
# Provider measures
# ---------------------------------------------------------------------------

class TestBoardCertification:
    def test_certified(self):
        m = BoardCertificationMeasure()
        assert m.normalize(1.0) == 1.0

    def test_not_certified(self):
        m = BoardCertificationMeasure()
        assert m.normalize(0.0) == 0.0

    def test_evaluate_missing(self):
        m = BoardCertificationMeasure()
        inp = MeasureInput(name="board_certification", value=None, missing=True)
        trace = m.evaluate(inp, weight=0.2)
        assert trace.missing_data is True
        assert trace.normalized_value is None


class TestYearsInPractice:
    def test_zero_years(self):
        m = YearsInPracticeMeasure()
        assert m.normalize(0.0) == 0.0

    def test_fifteen_years(self):
        m = YearsInPracticeMeasure()
        assert m.normalize(15.0) == 0.5

    def test_thirty_years(self):
        m = YearsInPracticeMeasure()
        assert m.normalize(30.0) == 1.0

    def test_over_thirty(self):
        m = YearsInPracticeMeasure()
        assert m.normalize(50.0) == 1.0

    def test_negative(self):
        m = YearsInPracticeMeasure()
        assert m.normalize(-5.0) == 0.0


class TestSpecialtyMatch:
    def test_exact_match(self):
        m = SpecialtyMatchMeasure()
        assert m.normalize(2.0) == 1.0

    def test_related(self):
        m = SpecialtyMatchMeasure()
        assert m.normalize(1.0) == 0.5

    def test_no_match(self):
        m = SpecialtyMatchMeasure()
        assert m.normalize(0.0) == 0.0


class TestPrescriptionBehavior:
    def test_full_adherence(self):
        m = PrescriptionBehaviorMeasure()
        assert m.normalize(100.0) == 1.0

    def test_half_adherence(self):
        m = PrescriptionBehaviorMeasure()
        assert m.normalize(50.0) == 0.5

    def test_zero(self):
        m = PrescriptionBehaviorMeasure()
        assert m.normalize(0.0) == 0.0

    def test_over_hundred(self):
        m = PrescriptionBehaviorMeasure()
        assert m.normalize(120.0) == 1.0


class TestPatientRatings:
    def test_five_stars(self):
        m = PatientRatingsMeasure()
        assert m.normalize(5.0) == 1.0

    def test_three_stars(self):
        m = PatientRatingsMeasure()
        assert m.normalize(3.0) == pytest.approx(0.6)

    def test_zero_stars(self):
        m = PatientRatingsMeasure()
        assert m.normalize(0.0) == 0.0


class TestNewPatientAcceptance:
    def test_accepting(self):
        m = NewPatientAcceptanceMeasure()
        assert m.normalize(1.0) == 1.0

    def test_not_accepting(self):
        m = NewPatientAcceptanceMeasure()
        assert m.normalize(0.0) == 0.0


# ---------------------------------------------------------------------------
# Clinical trial measures
# ---------------------------------------------------------------------------

class TestTrialPhase:
    def test_phase_3(self):
        m = TrialPhaseMeasure()
        assert m.normalize(3.0) == 1.0

    def test_phase_2(self):
        m = TrialPhaseMeasure()
        assert m.normalize(2.0) == 0.75

    def test_phase_1(self):
        m = TrialPhaseMeasure()
        assert m.normalize(1.0) == 0.5

    def test_phase_0(self):
        m = TrialPhaseMeasure()
        assert m.normalize(0.0) == 0.25


class TestTrialRecruitment:
    def test_actively_recruiting(self):
        m = TrialRecruitmentMeasure()
        assert m.normalize(3.0) == 1.0

    def test_not_yet(self):
        m = TrialRecruitmentMeasure()
        assert m.normalize(2.0) == 0.75

    def test_by_invitation(self):
        m = TrialRecruitmentMeasure()
        assert m.normalize(1.0) == 0.5

    def test_completed(self):
        m = TrialRecruitmentMeasure()
        assert m.normalize(0.0) == 0.0


class TestTrialConditionRelevance:
    def test_high_relevance(self):
        m = TrialConditionRelevanceMeasure()
        assert m.normalize(0.95) == 0.95

    def test_clamped_above(self):
        m = TrialConditionRelevanceMeasure()
        assert m.normalize(1.5) == 1.0

    def test_clamped_below(self):
        m = TrialConditionRelevanceMeasure()
        assert m.normalize(-0.1) == 0.0


class TestTrialSponsor:
    def test_nih(self):
        m = TrialSponsorMeasure()
        assert m.normalize(3.0) == 1.0

    def test_large_pharma(self):
        m = TrialSponsorMeasure()
        assert m.normalize(2.0) == 0.75

    def test_unknown(self):
        m = TrialSponsorMeasure()
        assert m.normalize(0.0) == 0.25


class TestTrialProximity:
    def test_zero_distance(self):
        m = TrialProximityMeasure()
        assert m.normalize(0.0) == 1.0

    def test_fifty_miles(self):
        m = TrialProximityMeasure()
        assert m.normalize(50.0) == 0.5

    def test_hundred_miles(self):
        m = TrialProximityMeasure()
        assert m.normalize(100.0) == 0.0

    def test_over_hundred(self):
        m = TrialProximityMeasure()
        assert m.normalize(200.0) == 0.0


class TestTrialRecency:
    def test_today(self):
        m = TrialRecencyMeasure()
        assert m.normalize(0.0) == 1.0

    def test_half_year(self):
        m = TrialRecencyMeasure()
        assert m.normalize(182.5) == pytest.approx(0.5, abs=0.01)

    def test_over_year(self):
        m = TrialRecencyMeasure()
        assert m.normalize(400.0) == 0.0


class TestTrialEligibility:
    def test_full_match(self):
        m = TrialEligibilityMeasure()
        assert m.normalize(100.0) == 1.0

    def test_partial_match(self):
        m = TrialEligibilityMeasure()
        assert m.normalize(60.0) == pytest.approx(0.6)

    def test_no_match(self):
        m = TrialEligibilityMeasure()
        assert m.normalize(0.0) == 0.0


# ---------------------------------------------------------------------------
# Failsafe — evaluate method on all measures
# ---------------------------------------------------------------------------

class TestEvaluateFailsafe:
    @pytest.mark.parametrize("MeasureCls", [
        BoardCertificationMeasure,
        YearsInPracticeMeasure,
        SpecialtyMatchMeasure,
        PrescriptionBehaviorMeasure,
        PatientRatingsMeasure,
        NewPatientAcceptanceMeasure,
        TrialPhaseMeasure,
        TrialRecruitmentMeasure,
        TrialConditionRelevanceMeasure,
        TrialSponsorMeasure,
        TrialProximityMeasure,
        TrialRecencyMeasure,
        TrialEligibilityMeasure,
    ])
    def test_missing_value_handled(self, MeasureCls):
        m = MeasureCls()
        inp = MeasureInput(name=m.name, value=None, missing=True)
        trace = m.evaluate(inp, weight=0.5)
        assert trace.missing_data is True
        assert trace.normalized_value is None
        assert trace.weighted_contribution is None

    @pytest.mark.parametrize("MeasureCls", [
        BoardCertificationMeasure,
        YearsInPracticeMeasure,
        SpecialtyMatchMeasure,
        PrescriptionBehaviorMeasure,
        PatientRatingsMeasure,
        NewPatientAcceptanceMeasure,
        TrialPhaseMeasure,
        TrialRecruitmentMeasure,
        TrialConditionRelevanceMeasure,
        TrialSponsorMeasure,
        TrialProximityMeasure,
        TrialRecencyMeasure,
        TrialEligibilityMeasure,
    ])
    def test_valid_value_returns_score(self, MeasureCls):
        m = MeasureCls()
        inp = MeasureInput(name=m.name, value=1.0)
        trace = m.evaluate(inp, weight=0.5)
        assert trace.missing_data is False
        assert trace.normalized_value is not None
        assert 0.0 <= trace.normalized_value <= 1.0
