# Copyright (c) 2026 Skip Snow. All rights reserved.
# Tests for EVAL-WEIGHTS.

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from evaluate_care.weights import (
    DEFAULT_PROVIDER_WEIGHTS,
    DEFAULT_CLINICAL_TRIAL_WEIGHTS,
    validate_weights_sum,
    redistribute_weights,
)
from evaluate_care.models import WeightConfig, WeightEntry


class TestDefaultWeights:
    def test_provider_weights_sum_to_one(self):
        total = sum(w.weight for w in DEFAULT_PROVIDER_WEIGHTS.weights)
        assert abs(total - 1.0) < 1e-9

    def test_clinical_trial_weights_sum_to_one(self):
        total = sum(w.weight for w in DEFAULT_CLINICAL_TRIAL_WEIGHTS.weights)
        assert abs(total - 1.0) < 1e-9

    def test_provider_weights_all_positive(self):
        for w in DEFAULT_PROVIDER_WEIGHTS.weights:
            assert w.weight > 0

    def test_trial_weights_all_positive(self):
        for w in DEFAULT_CLINICAL_TRIAL_WEIGHTS.weights:
            assert w.weight > 0


class TestValidateWeightsSum:
    def test_valid(self):
        cfg = WeightConfig(weights=[WeightEntry(measure_name="a", weight=0.5)])
        assert validate_weights_sum(cfg) is True

    def test_zero_sum(self):
        cfg = WeightConfig(weights=[WeightEntry(measure_name="a", weight=0.0)])
        assert validate_weights_sum(cfg) is False

    def test_empty(self):
        cfg = WeightConfig(weights=[])
        assert validate_weights_sum(cfg) is False


class TestRedistributeWeights:
    def test_no_exclusions(self):
        result = redistribute_weights(DEFAULT_PROVIDER_WEIGHTS, set())
        total = sum(result.values())
        assert abs(total - 1.0) < 1e-9

    def test_with_exclusions(self):
        result = redistribute_weights(
            DEFAULT_PROVIDER_WEIGHTS,
            {"years_in_practice", "new_patient_acceptance"},
        )
        assert "years_in_practice" not in result
        assert "new_patient_acceptance" not in result
        total = sum(result.values())
        assert abs(total - 1.0) < 1e-9

    def test_all_excluded_raises(self):
        all_names = {w.measure_name for w in DEFAULT_PROVIDER_WEIGHTS.weights}
        with pytest.raises(ValueError, match="zero"):
            redistribute_weights(DEFAULT_PROVIDER_WEIGHTS, all_names)

    def test_proportional_redistribution(self):
        cfg = WeightConfig(weights=[
            WeightEntry(measure_name="a", weight=0.6),
            WeightEntry(measure_name="b", weight=0.2),
            WeightEntry(measure_name="c", weight=0.2),
        ])
        result = redistribute_weights(cfg, {"c"})
        # a should be 0.6/0.8 = 0.75, b should be 0.2/0.8 = 0.25
        assert abs(result["a"] - 0.75) < 1e-9
        assert abs(result["b"] - 0.25) < 1e-9
