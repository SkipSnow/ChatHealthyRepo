# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-CT-MEASURE-1 — Clinical Trial Phase measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialPhaseMeasure(BaseMeasure):
    """Trial phase scoring.

    Phase 3 = 1.0, Phase 2 = 0.75, Phase 1 = 0.5, Phase 0/other = 0.25.
    Input encoding: 0, 1, 2, 3, 4.
    """

    name = "trial_phase"
    normalization_method = NormalizationMethod.CATEGORICAL

    _PHASE_SCORES = {
        4.0: 1.0,   # Phase 4 (post-market)
        3.0: 1.0,   # Phase 3
        2.0: 0.75,  # Phase 2
        1.0: 0.5,   # Phase 1
        0.0: 0.25,  # Phase 0 / early
    }

    def normalize(self, raw_value: float) -> float:
        return self._PHASE_SCORES.get(float(int(raw_value)), 0.25)
