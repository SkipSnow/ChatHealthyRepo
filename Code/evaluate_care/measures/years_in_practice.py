# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-P-MEASURE-2 — Years in Practice measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class YearsInPracticeMeasure(BaseMeasure):
    """Linear scale: 0 years -> 0.0, 30+ years -> 1.0."""

    name = "years_in_practice"
    normalization_method = NormalizationMethod.LINEAR_SCALE
    MAX_YEARS = 30.0

    def normalize(self, raw_value: float) -> float:
        if raw_value <= 0:
            return 0.0
        return min(raw_value / self.MAX_YEARS, 1.0)
