# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-P-MEASURE-5 — New Patient Acceptance measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class NewPatientAcceptanceMeasure(BaseMeasure):
    """Boolean measure: 1.0 if accepting new patients, 0.0 otherwise."""

    name = "new_patient_acceptance"
    normalization_method = NormalizationMethod.BOOLEAN

    def normalize(self, raw_value: float) -> float:
        return 1.0 if raw_value >= 1.0 else 0.0
