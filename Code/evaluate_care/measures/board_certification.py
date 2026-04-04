# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-P-MEASURE-1 — Board Certification measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class BoardCertificationMeasure(BaseMeasure):
    """Boolean measure: 1.0 if board-certified, 0.0 otherwise."""

    name = "board_certification"
    normalization_method = NormalizationMethod.BOOLEAN

    def normalize(self, raw_value: float) -> float:
        return 1.0 if raw_value >= 1.0 else 0.0
