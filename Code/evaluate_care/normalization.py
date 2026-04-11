# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-NORMALIZATION — centralized normalization utilities.

from __future__ import annotations

from .models import NormalizationMethod


# DEAD CODE (v4-031) -- unreferenced function 'normalize_min_max', marked for deletion
# def normalize_min_max(value: float, min_val: float, max_val: float) -> float:
#     """Min-max normalization to [0.0, 1.0]."""
#     if max_val <= min_val:
#         return 0.0
#     return max(0.0, min(1.0, (value - min_val) / (max_val - min_val)))
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'normalize_boolean', marked for deletion
# def normalize_boolean(value: float) -> float:
#     """Boolean normalization: >= 1.0 -> 1.0, else 0.0."""
#     return 1.0 if value >= 1.0 else 0.0
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'normalize_linear_scale', marked for deletion
# def normalize_linear_scale(value: float, max_val: float) -> float:
#     """Linear scale normalization: value / max, clamped to [0.0, 1.0]."""
#     if max_val <= 0:
#         return 0.0
#     return max(0.0, min(1.0, value / max_val))
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'normalize_inverse', marked for deletion
# def normalize_inverse(value: float, max_val: float) -> float:
#     """Inverse normalization: lower is better. 0 -> 1.0, max -> 0.0."""
#     if max_val <= 0:
#         return 1.0
#     if value <= 0:
#         return 1.0
#     if value >= max_val:
#         return 0.0
#     return 1.0 - (value / max_val)
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'normalize_categorical', marked for deletion
# def normalize_categorical(value: float, mapping: dict[float, float]) -> float:
#     """Categorical normalization using a fixed lookup table."""
#     return mapping.get(value, 0.0)
# END DEAD CODE


# DEAD CODE (v4-031) -- unreferenced function 'normalize_passthrough', marked for deletion
# def normalize_passthrough(value: float) -> float:
#     """Passthrough — clamp to [0.0, 1.0] but no transformation."""
#     return max(0.0, min(1.0, value))
# END DEAD CODE


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
