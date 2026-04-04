# Copyright (c) 2026 Skip Snow. All rights reserved.
# Evaluate Care — individual measure implementations.

from .board_certification import BoardCertificationMeasure
from .new_patient_acceptance import NewPatientAcceptanceMeasure
from .patient_ratings import PatientRatingsMeasure
from .prescription_behavior import PrescriptionBehaviorMeasure
from .specialty_match import SpecialtyMatchMeasure
from .trial_condition_relevance import TrialConditionRelevanceMeasure
from .trial_eligibility import TrialEligibilityMeasure
from .trial_phase import TrialPhaseMeasure
from .trial_proximity import TrialProximityMeasure
from .trial_recency import TrialRecencyMeasure
from .trial_recruitment import TrialRecruitmentMeasure
from .trial_sponsor import TrialSponsorMeasure
from .years_in_practice import YearsInPracticeMeasure

PROVIDER_MEASURES = [
    BoardCertificationMeasure,
    YearsInPracticeMeasure,
    SpecialtyMatchMeasure,
    PrescriptionBehaviorMeasure,
    PatientRatingsMeasure,
    NewPatientAcceptanceMeasure,
]

CLINICAL_TRIAL_MEASURES = [
    TrialPhaseMeasure,
    TrialRecruitmentMeasure,
    TrialConditionRelevanceMeasure,
    TrialSponsorMeasure,
    TrialProximityMeasure,
    TrialRecencyMeasure,
    TrialEligibilityMeasure,
]

__all__ = [
    "PROVIDER_MEASURES",
    "CLINICAL_TRIAL_MEASURES",
    "BoardCertificationMeasure",
    "YearsInPracticeMeasure",
    "SpecialtyMatchMeasure",
    "PrescriptionBehaviorMeasure",
    "PatientRatingsMeasure",
    "NewPatientAcceptanceMeasure",
    "TrialPhaseMeasure",
    "TrialRecruitmentMeasure",
    "TrialConditionRelevanceMeasure",
    "TrialSponsorMeasure",
    "TrialProximityMeasure",
    "TrialRecencyMeasure",
    "TrialEligibilityMeasure",
]
