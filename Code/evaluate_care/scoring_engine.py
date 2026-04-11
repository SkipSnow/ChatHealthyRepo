# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# EVAL-SCORE-PROV / EVAL-SCORE-CT — composite scoring engine.
#
# Deterministic: same input always yields same output.
# No AI-generated scores — system decides, not the model (GOV-011).

from __future__ import annotations

import logging
from typing import Dict, Optional, Type

from .cache import ScoringCache
from .confidence import compute_confidence
from .failsafe import build_failsafe_response, is_scorable
from .measures.base import BaseMeasure
from .measures import PROVIDER_MEASURES, CLINICAL_TRIAL_MEASURES
from .models import (
    EntityType,
    MeasureInput,
    MeasureResult,
    MeasureTrace,
    ScoringRequest,
    ScoringResponse,
)
from .provenance import build_provenance
from .weights import redistribute_weights

logger = logging.getLogger(__name__)


class ScoringEngine:
    """Deterministic composite scoring engine for providers and clinical trials.

    Usage::

        engine = ScoringEngine()
        response = engine.score(request)
    """

    def __init__(self, cache: Optional[ScoringCache] = None):
        self._cache = cache or ScoringCache()
        self._measure_registry: Dict[str, BaseMeasure] = {}
        self._register_defaults()

    # ------------------------------------------------------------------
    # Registry
    # ------------------------------------------------------------------

    def _register_defaults(self) -> None:
        for cls in PROVIDER_MEASURES + CLINICAL_TRIAL_MEASURES:
            instance = cls()
            self._measure_registry[instance.name] = instance
    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def score(
        self,
        request: ScoringRequest,
        *,
        use_cache: bool = True,
    ) -> ScoringResponse:
        """Compute a deterministic composite score.

        1. Check cache
        2. Validate inputs (failsafe)
        3. Evaluate each measure
        4. Redistribute weights for missing measures
        5. Compute weighted aggregate
        6. Attach provenance + confidence
        7. Cache and return
        """
        # 1. Cache lookup
        if use_cache:
            cached = self._cache.get(request)
            if cached is not None:
                logger.debug("Cache hit for entity %s", request.entity_id)
                return cached

        # 2. Failsafe — at least one measure required
        if not is_scorable(request.measures):
            resp = build_failsafe_response(
                entity_id=request.entity_id,
                entity_type=request.entity_type,
                reason="All measures are missing or invalid.",
                error_code="EVAL_NO_DATA",
            )
            return resp

        # 3. Evaluate each measure
        weight_map = request.weights.as_dict()
        traces: list[MeasureTrace] = []
        for m_input in request.measures:
            measure_impl = self._measure_registry.get(m_input.name)
            w = weight_map.get(m_input.name, 0.0)
            if measure_impl is None:
                # Unknown measure — treat as missing
                traces.append(
                    MeasureTrace(
                        measure_name=m_input.name,
                        raw_value=m_input.raw_value,
                        normalized_value=None,
                        weight_applied=w,
                        weighted_contribution=None,
                        missing_data=True,
                        source=m_input.source,
                    )
                )
                continue
            trace = measure_impl.evaluate(m_input, w)
            traces.append(trace)

        # 4. Redistribute weights for missing measures
        missing_names = {t.measure_name for t in traces if t.missing_data}
        available_traces = [t for t in traces if not t.missing_data]

        if not available_traces:
            resp = build_failsafe_response(
                entity_id=request.entity_id,
                entity_type=request.entity_type,
                reason="All measures failed evaluation.",
                error_code="EVAL_ALL_FAILED",
            )
            resp.trace = traces
            resp.confidence = compute_confidence(traces)
            resp.provenance = build_provenance(request)
            return resp

        try:
            adjusted_weights = redistribute_weights(request.weights, missing_names)
        except ValueError as exc:
            resp = build_failsafe_response(
                entity_id=request.entity_id,
                entity_type=request.entity_type,
                reason=str(exc),
                error_code="EVAL_ZERO_WEIGHTS",
            )
            resp.trace = traces
            resp.provenance = build_provenance(request)
            return resp

        # 5. Compute weighted aggregate
        composite = 0.0
        for trace in available_traces:
            adj_w = adjusted_weights.get(trace.measure_name, 0.0)
            contribution = round((trace.normalized_value or 0.0) * adj_w, 6)
            trace.weighted_contribution = contribution
            trace.weight_applied = adj_w
            composite += contribution

        composite = round(max(0.0, min(1.0, composite)), 4)

        # 6. Build response
        measures_out = []
        for trace in traces:
            measures_out.append(
                MeasureResult(
                    name=trace.measure_name,
                    raw_value=trace.raw_value,
                    normalized_score=trace.normalized_value,
                    weight=trace.weight_applied,
                    weighted_contribution=trace.weighted_contribution,
                    normalization_method=trace.normalization_method,
                    missing_data=trace.missing_data,
                    source=trace.source,
                )
            )

        confidence = compute_confidence(traces)
        provenance = build_provenance(request)

        response = ScoringResponse(
            entity_id=request.entity_id,
            entity_type=request.entity_type,
            composite_score=composite,
            measures=measures_out,
            trace=traces,
            confidence=confidence,
            provenance=provenance,
        )

        # 7. Cache
        if use_cache:
            self._cache.put(request, response)

        return response

    # ------------------------------------------------------------------
    # Convenience — score with default weights
    # ------------------------------------------------------------------

    def score_provider(
        self,
        provider_id: str,
        measures: list[MeasureInput],
        *,
        use_cache: bool = True,
    ) -> ScoringResponse:
        """Score a provider using default provider weights."""
        from .weights import DEFAULT_PROVIDER_WEIGHTS

        request = ScoringRequest(
            entity_id=provider_id,
            entity_type=EntityType.PROVIDER,
            measures=measures,
            weights=DEFAULT_PROVIDER_WEIGHTS,
        )
        return self.score(request, use_cache=use_cache)

    def score_clinical_trial(
        self,
        trial_id: str,
        measures: list[MeasureInput],
        *,
        use_cache: bool = True,
    ) -> ScoringResponse:
        """Score a clinical trial using default trial weights."""
        from .weights import DEFAULT_CLINICAL_TRIAL_WEIGHTS

        request = ScoringRequest(
            entity_id=trial_id,
            entity_type=EntityType.CLINICAL_TRIAL,
            measures=measures,
            weights=DEFAULT_CLINICAL_TRIAL_WEIGHTS,
        )
        return self.score(request, use_cache=use_cache)

    @property
    def cache(self) -> ScoringCache:
        return self._cache
