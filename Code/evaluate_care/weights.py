# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-WEIGHTS — default weight configurations and utilities.

from __future__ import annotations

from .models import WeightConfig, WeightEntry


# ---------------------------------------------------------------------------
# Default provider weights (EVAL-P-MEASURE-1 through 7 + EVAL-P-RX)
# ---------------------------------------------------------------------------

DEFAULT_PROVIDER_WEIGHTS = WeightConfig(
    version="1.0",
    weights=[
        WeightEntry(measure_name="board_certification", weight=0.20),
        WeightEntry(measure_name="years_in_practice", weight=0.10),
        WeightEntry(measure_name="specialty_match", weight=0.25),
        WeightEntry(measure_name="prescription_behavior", weight=0.15),
        WeightEntry(measure_name="patient_ratings", weight=0.20),
        WeightEntry(measure_name="new_patient_acceptance", weight=0.10),
    ],
)

# ---------------------------------------------------------------------------
# Default clinical-trial weights (EVAL-CT-MEASURE-1 through 7)
# ---------------------------------------------------------------------------

DEFAULT_CLINICAL_TRIAL_WEIGHTS = WeightConfig(
    version="1.0",
    weights=[
        WeightEntry(measure_name="trial_phase", weight=0.15),
        WeightEntry(measure_name="trial_recruitment", weight=0.15),
        WeightEntry(measure_name="trial_condition_relevance", weight=0.25),
        WeightEntry(measure_name="trial_sponsor", weight=0.10),
        WeightEntry(measure_name="trial_proximity", weight=0.15),
        WeightEntry(measure_name="trial_recency", weight=0.10),
        WeightEntry(measure_name="trial_eligibility", weight=0.10),
    ],
)


def validate_weights_sum(config: WeightConfig) -> bool:
    """Return True if the sum of all weights is > 0."""
    return sum(w.weight for w in config.weights) > 0


def redistribute_weights(config: WeightConfig, exclude: set[str]) -> dict[str, float]:
    """Redistribute weights proportionally after excluding missing measures.

    Returns a dict of measure_name -> adjusted weight.
    Raises ValueError if all remaining weights sum to zero.
    """
    remaining = {
        w.measure_name: w.weight
        for w in config.weights
        if w.measure_name not in exclude and w.weight > 0
    }
    total = sum(remaining.values())
    if total <= 0:
        raise ValueError("All weights sum to zero after excluding missing measures.")
    return {name: w / total for name, w in remaining.items()}
