# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-CT-MEASURE-7 — Clinical Trial Eligibility Match measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialEligibilityMeasure(BaseMeasure):
    """Eligibility criteria match score.

    Input: percentage of eligibility criteria met (0-100).
    Output: normalized to [0.0, 1.0].
    """

    name = "trial_eligibility"
    normalization_method = NormalizationMethod.LINEAR_SCALE

    def normalize(self, raw_value: float) -> float:
        if raw_value <= 0:
            return 0.0
        return min(raw_value / 100.0, 1.0)
