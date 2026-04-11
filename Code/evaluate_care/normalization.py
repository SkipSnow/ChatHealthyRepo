# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-NORMALIZATION — centralized normalization utilities.

from __future__ import annotations

from .models import NormalizationMethod





def describe_method(method: NormalizationMethod) -> str:
    """Return human-readable description of a normalization method."""
    descriptions = {
        NormalizationMethod.MIN_MAX: "Scaled linearly between minimum and maximum observed values",
        NormalizationMethod.BOOLEAN: "Binary: 1.0 if present/true, 0.0 otherwise",
        NormalizationMethod.CATEGORICAL: "Mapped from categorical tiers to fixed score bands",
        NormalizationMethod.LINEAR_SCALE: "Scaled linearly from 0 to a domain-specific maximum",
        NormalizationMethod.INVERSE: "Inverse scale: lower raw values produce higher scores",
        NormalizationMethod.PASSTHROUGH: "Raw value used directly (already in 0-1 range)",
    }
    return descriptions.get(method, "Unknown normalization method")
