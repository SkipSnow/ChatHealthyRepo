# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-CT-MEASURE-5 — Clinical Trial Proximity measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialProximityMeasure(BaseMeasure):
    """Distance-based scoring — closer is better.

    Input: distance in miles.
    Output: inverse scale: 0 miles = 1.0, 100+ miles = 0.0.
    """

    name = "trial_proximity"
    normalization_method = NormalizationMethod.INVERSE
    MAX_DISTANCE = 100.0

    def normalize(self, raw_value: float) -> float:
        if raw_value <= 0:
            return 1.0
        if raw_value >= self.MAX_DISTANCE:
            return 0.0
        return 1.0 - (raw_value / self.MAX_DISTANCE)
