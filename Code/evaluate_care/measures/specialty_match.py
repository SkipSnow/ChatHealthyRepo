# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-P-MEASURE-3 — Specialty Match measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class SpecialtyMatchMeasure(BaseMeasure):
    """Categorical: 1.0 = exact match, 0.5 = related, 0.0 = no match.

    Input value encoding:
        2.0 = exact match
        1.0 = related specialty
        0.0 = no match
    """

    name = "specialty_match"
    normalization_method = NormalizationMethod.CATEGORICAL

    def normalize(self, raw_value: float) -> float:
        if raw_value >= 2.0:
            return 1.0
        if raw_value >= 1.0:
            return 0.5
        return 0.0
