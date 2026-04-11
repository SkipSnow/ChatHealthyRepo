# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Tests for EVAL-NORMALIZATION.

import pytest
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from evaluate_care.normalization import (
    normalize_min_max,
    normalize_boolean,
    normalize_linear_scale,
    normalize_inverse,
    normalize_categorical,
    normalize_passthrough,
    describe_method,
)
from evaluate_care.models import NormalizationMethod


class TestMinMax:
    def test_midpoint(self):
        assert normalize_min_max(50, 0, 100) == 0.5

    def test_at_min(self):
        assert normalize_min_max(0, 0, 100) == 0.0

    def test_at_max(self):
        assert normalize_min_max(100, 0, 100) == 1.0

    def test_below_min_clamped(self):
        assert normalize_min_max(-10, 0, 100) == 0.0

    def test_above_max_clamped(self):
        assert normalize_min_max(200, 0, 100) == 1.0

    def test_equal_min_max(self):
        assert normalize_min_max(5, 5, 5) == 0.0


class TestBoolean:
    def test_true(self):
        assert normalize_boolean(1.0) == 1.0

    def test_false(self):
        assert normalize_boolean(0.0) == 0.0

    def test_above_one(self):
        assert normalize_boolean(2.5) == 1.0


class TestLinearScale:
    def test_half(self):
        assert normalize_linear_scale(50, 100) == 0.5

    def test_zero_max(self):
        assert normalize_linear_scale(5, 0) == 0.0

    def test_negative_input(self):
        assert normalize_linear_scale(-5, 100) == 0.0


class TestInverse:
    def test_zero_distance(self):
        assert normalize_inverse(0, 100) == 1.0

    def test_max_distance(self):
        assert normalize_inverse(100, 100) == 0.0

    def test_half_distance(self):
        assert normalize_inverse(50, 100) == 0.5

    def test_over_max(self):
        assert normalize_inverse(200, 100) == 0.0


class TestCategorical:
    def test_known_key(self):
        mapping = {1.0: 0.5, 2.0: 1.0}
        assert normalize_categorical(2.0, mapping) == 1.0

    def test_unknown_key(self):
        mapping = {1.0: 0.5}
        assert normalize_categorical(9.0, mapping) == 0.0


class TestPassthrough:
    def test_in_range(self):
        assert normalize_passthrough(0.7) == 0.7

    def test_over_one(self):
        assert normalize_passthrough(1.5) == 1.0

    def test_negative(self):
        assert normalize_passthrough(-0.3) == 0.0


class TestDescribeMethod:
    @pytest.mark.parametrize("method", list(NormalizationMethod))
    def test_all_methods_have_description(self, method):
        desc = describe_method(method)
        assert isinstance(desc, str)
        assert len(desc) > 10
