# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Base class for all Evaluate Care measures.

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

from ..models import MeasureInput, MeasureTrace, NormalizationMethod


class BaseMeasure(ABC):
    """Abstract base for every scoring measure.

    Subclasses implement ``normalize`` which maps a raw value to [0.0, 1.0].
    The base class provides the failsafe wrapper (EVAL-FAILSAFE).
    """

    name: str = ""
    normalization_method: NormalizationMethod = NormalizationMethod.PASSTHROUGH
    default_on_missing: float = 0.0

    @abstractmethod
    def normalize(self, raw_value: float) -> float:
        """Return a normalized score in [0.0, 1.0]."""
        ...

    # ------------------------------------------------------------------
    # EVAL-FAILSAFE — graceful missing-data handling
    # ------------------------------------------------------------------
    def evaluate(self, measure_input: MeasureInput, weight: float) -> MeasureTrace:
        """Score a measure with full failsafe handling."""
        if measure_input.missing or measure_input.value is None:
            return MeasureTrace(
                measure_name=self.name,
                raw_value=measure_input.raw_value,
                normalized_value=None,
                weight_applied=weight,
                weighted_contribution=None,
                normalization_method=self.normalization_method,
                missing_data=True,
                source=measure_input.source,
            )

        try:
            normalized = self._clamp(self.normalize(measure_input.value))
            contribution = round(normalized * weight, 6)
        except Exception:
            return MeasureTrace(
                measure_name=self.name,
                raw_value=measure_input.raw_value,
                normalized_value=None,
                weight_applied=weight,
                weighted_contribution=None,
                normalization_method=self.normalization_method,
                missing_data=True,
                source=measure_input.source,
            )

        return MeasureTrace(
            measure_name=self.name,
            raw_value=measure_input.raw_value if measure_input.raw_value is not None else measure_input.value,
            normalized_value=round(normalized, 6),
            weight_applied=weight,
            weighted_contribution=contribution,
            normalization_method=self.normalization_method,
            missing_data=False,
            source=measure_input.source,
        )

    @staticmethod
    def _clamp(v: float) -> float:
        return max(0.0, min(1.0, v))
