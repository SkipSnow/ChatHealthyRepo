# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-P-MEASURE-4 — Patient Ratings measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class PatientRatingsMeasure(BaseMeasure):
    """Star-rating scale: input 0-5, normalized to [0.0, 1.0]."""

    name = "patient_ratings"
    normalization_method = NormalizationMethod.LINEAR_SCALE
    MAX_RATING = 5.0

    def normalize(self, raw_value: float) -> float:
        if raw_value <= 0:
            return 0.0
        return min(raw_value / self.MAX_RATING, 1.0)
