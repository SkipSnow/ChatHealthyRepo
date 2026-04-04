# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-CT-MEASURE-6 — Clinical Trial Recency measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialRecencyMeasure(BaseMeasure):
    """How recently the trial was updated / started.

    Input: days since last update.
    Output: inverse scale — 0 days = 1.0, 365+ days = 0.0.
    """

    name = "trial_recency"
    normalization_method = NormalizationMethod.INVERSE
    MAX_DAYS = 365.0

    def normalize(self, raw_value: float) -> float:
        if raw_value <= 0:
            return 1.0
        if raw_value >= self.MAX_DAYS:
            return 0.0
        return 1.0 - (raw_value / self.MAX_DAYS)
