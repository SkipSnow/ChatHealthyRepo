# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-P-RX — Prescription Behavior measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class PrescriptionBehaviorMeasure(BaseMeasure):
    """Guideline adherence score.

    Input: adherence percentage 0-100.
    Output: normalized to [0.0, 1.0].
    """

    name = "prescription_behavior"
    normalization_method = NormalizationMethod.LINEAR_SCALE

    def normalize(self, raw_value: float) -> float:
        if raw_value <= 0:
            return 0.0
        return min(raw_value / 100.0, 1.0)
