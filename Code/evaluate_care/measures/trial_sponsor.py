# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-CT-MEASURE-4 — Clinical Trial Sponsor Credibility measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialSponsorMeasure(BaseMeasure):
    """Sponsor credibility tier.

    Encoding:
        3.0 = NIH / major academic center -> 1.0
        2.0 = large pharma -> 0.75
        1.0 = mid-tier / biotech -> 0.5
        0.0 = unknown / other -> 0.25
    """

    name = "trial_sponsor"
    normalization_method = NormalizationMethod.CATEGORICAL

    _TIER_SCORES = {
        3.0: 1.0,
        2.0: 0.75,
        1.0: 0.5,
        0.0: 0.25,
    }

    def normalize(self, raw_value: float) -> float:
        return self._TIER_SCORES.get(float(int(raw_value)), 0.25)
