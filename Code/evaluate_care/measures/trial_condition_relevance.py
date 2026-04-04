# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-CT-MEASURE-3 — Clinical Trial Condition Relevance measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialConditionRelevanceMeasure(BaseMeasure):
    """How relevant is the trial to the patient's condition.

    Input: similarity score 0.0 - 1.0 (pre-computed, e.g. embedding cosine).
    Output: passthrough (already normalized).
    """

    name = "trial_condition_relevance"
    normalization_method = NormalizationMethod.PASSTHROUGH

    def normalize(self, raw_value: float) -> float:
        return max(0.0, min(1.0, raw_value))
