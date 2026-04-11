# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-EXPLAIN — human-readable score explanations.

from __future__ import annotations

from . import normalization as norm_utils
from .models import (
    ConfidenceIndicator,
    EntityType,
    MeasureTrace,
    ScoreExplanation,
    ScoringResponse,
)


def explain_score(response: ScoringResponse) -> ScoreExplanation:
    """Generate a deterministic, human-readable explanation of a score.

    No AI/LLM is used — the explanation is built from templates (GOV-011).
    """
    if response.error:
        return ScoreExplanation(
            entity_id=response.entity_id,
            entity_type=response.entity_type,
            composite_score=None,
            summary=f"Scoring failed: {response.error}",
            measure_explanations=[],
            confidence=response.confidence,
        )

    measure_explanations = []
    for trace in response.trace:
        entry = {
            "measure": trace.measure_name,
            "raw_value": trace.raw_value,
            "normalized_score": trace.normalized_value,
            "weight": trace.weight_applied,
            "contribution": trace.weighted_contribution,
            "missing": trace.missing_data,
        }
        if trace.normalization_method:
            entry["normalization"] = norm_utils.describe_method(
                trace.normalization_method
            )
        if trace.missing_data:
            entry["note"] = (
                f"'{trace.measure_name}' data was unavailable; "
                "excluded from composite score."
            )
        measure_explanations.append(entry)

    # Build summary
    available = [t for t in response.trace if not t.missing_data]
    missing = [t for t in response.trace if t.missing_data]

    parts = []
    if response.composite_score is not None:
        parts.append(
            f"Composite score: {response.composite_score:.2f} "
            f"(based on {len(available)} of {len(response.trace)} measures)."
        )
    if missing:
        names = ", ".join(t.measure_name for t in missing)
        parts.append(f"Missing data for: {names}.")
    if response.confidence:
        parts.append(f"Confidence: {response.confidence.level.value}.")

    summary = " ".join(parts) if parts else "No scoring data available."

    return ScoreExplanation(
        entity_id=response.entity_id,
        entity_type=response.entity_type,
        composite_score=response.composite_score,
        summary=summary,
        measure_explanations=measure_explanations,
        confidence=response.confidence,
    )
