# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-CONFIDENCE — confidence indicator computation.

from __future__ import annotations

from .models import ConfidenceIndicator, ConfidenceLevel, MeasureTrace


def compute_confidence(traces: list[MeasureTrace]) -> ConfidenceIndicator:
    """Compute a confidence indicator from scored measure traces.

    Thresholds:
        >= 0.80 completeness -> HIGH
        >= 0.50 completeness -> MEDIUM
        >= 0.25 completeness -> LOW
        <  0.25 completeness -> INSUFFICIENT
    """
    total = len(traces)
    if total == 0:
        return ConfidenceIndicator(
            level=ConfidenceLevel.INSUFFICIENT,
            measures_available=0,
            measures_total=0,
            completeness_ratio=0.0,
            missing_measures=[],
        )

    available = sum(1 for t in traces if not t.missing_data)
    missing_names = [t.measure_name for t in traces if t.missing_data]
    ratio = available / total

    if ratio >= 0.80:
        level = ConfidenceLevel.HIGH
    elif ratio >= 0.50:
        level = ConfidenceLevel.MEDIUM
    elif ratio >= 0.25:
        level = ConfidenceLevel.LOW
    else:
        level = ConfidenceLevel.INSUFFICIENT

    return ConfidenceIndicator(
        level=level,
        measures_available=available,
        measures_total=total,
        completeness_ratio=round(ratio, 4),
        missing_measures=missing_names,
    )
