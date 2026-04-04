# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-FAILSAFE — graceful degradation when data is missing or invalid.

from __future__ import annotations

from .models import (
    ConfidenceLevel,
    MeasureInput,
    MeasureResult,
    ScoringResponse,
    EntityType,
)


def is_scorable(measures: list[MeasureInput]) -> bool:
    """Return True if at least one measure has a non-null value."""
    return any(m.value is not None and not m.missing for m in measures)


def build_failsafe_response(
    entity_id: str,
    entity_type: EntityType,
    reason: str,
    error_code: str = "EVAL_NO_DATA",
) -> ScoringResponse:
    """Build a valid ScoringResponse when scoring cannot proceed."""
    return ScoringResponse(
        entity_id=entity_id,
        entity_type=entity_type,
        composite_score=None,
        measures=[],
        trace=[],
        confidence=None,
        error=reason,
        error_code=error_code,
    )
