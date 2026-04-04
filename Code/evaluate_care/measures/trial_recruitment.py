# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-CT-MEASURE-2 — Clinical Trial Recruitment Status measure.

from ..models import NormalizationMethod
from .base import BaseMeasure


class TrialRecruitmentMeasure(BaseMeasure):
    """Recruitment status scoring.

    Encoding:
        3.0 = actively recruiting -> 1.0
        2.0 = not yet recruiting -> 0.75
        1.0 = enrolling by invitation -> 0.5
        0.0 = completed / suspended / other -> 0.0
    """

    name = "trial_recruitment"
    normalization_method = NormalizationMethod.CATEGORICAL

    _STATUS_SCORES = {
        3.0: 1.0,
        2.0: 0.75,
        1.0: 0.5,
        0.0: 0.0,
    }

    def normalize(self, raw_value: float) -> float:
        return self._STATUS_SCORES.get(float(int(raw_value)), 0.0)
