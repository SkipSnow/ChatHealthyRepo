# Copyright (c) 2026 Skip Snow. All rights reserved.
# Pydantic models for the Evaluate Care scoring engine.
# Covers all input/output schemas for EVAL-SCORE-PROV, EVAL-SCORE-CT, and
# cross-cutting concerns (provenance, confidence, cache, explainability).

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class EntityType(str, Enum):
    PROVIDER = "provider"
    CLINICAL_TRIAL = "clinical_trial"


class NormalizationMethod(str, Enum):
    MIN_MAX = "min_max"
    BOOLEAN = "boolean"
    CATEGORICAL = "categorical"
    LINEAR_SCALE = "linear_scale"
    INVERSE = "inverse"
    PASSTHROUGH = "passthrough"


class ConfidenceLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INSUFFICIENT = "insufficient"


# ---------------------------------------------------------------------------
# Measure input
# ---------------------------------------------------------------------------

class MeasureInput(BaseModel):
    """A single quality measure with a name, value, and optional metadata."""
    name: str = Field(..., min_length=1)
    value: Optional[float] = None
    raw_value: Optional[Any] = None
    metadata: Optional[Dict[str, Any]] = None
    source: Optional[str] = None
    missing: bool = False

    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Weight map
# ---------------------------------------------------------------------------

class WeightEntry(BaseModel):
    """Weight for a single measure — must be non-negative."""
    measure_name: str
    weight: float = Field(..., ge=0.0)


class WeightConfig(BaseModel):
    """Complete weight configuration for a scoring request."""
    weights: List[WeightEntry]
    version: str = "1.0"

    def as_dict(self) -> Dict[str, float]:
        return {w.measure_name: w.weight for w in self.weights}


# ---------------------------------------------------------------------------
# Scoring request (input)
# ---------------------------------------------------------------------------

class ScoringRequest(BaseModel):
    """Top-level input for the scoring engine."""
    entity_id: str = Field(..., min_length=1)
    entity_type: EntityType
    measures: List[MeasureInput]
    weights: WeightConfig
    metadata: Optional[Dict[str, Any]] = None

    model_config = {"extra": "allow"}

    @model_validator(mode="after")
    def validate_weights_cover_measures(self) -> "ScoringRequest":
        weight_map = self.weights.as_dict()
        for m in self.measures:
            if not m.missing and m.name not in weight_map:
                raise ValueError(
                    f"Measure '{m.name}' has no corresponding weight entry."
                )
        return self


# ---------------------------------------------------------------------------
# Trace / provenance
# ---------------------------------------------------------------------------

class MeasureTrace(BaseModel):
    """Captures how a single measure was scored — full traceability."""
    measure_name: str
    raw_value: Optional[Any] = None
    normalized_value: Optional[float] = None
    weight_applied: float
    weighted_contribution: Optional[float] = None
    normalization_method: Optional[NormalizationMethod] = None
    missing_data: bool = False
    source: Optional[str] = None


class DataProvenance(BaseModel):
    """EVAL-DATA-PROVENANCE — origin metadata for every score."""
    engine_version: str = "0.1.4"
    scoring_timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    input_hash: Optional[str] = None
    measure_sources: Dict[str, Optional[str]] = Field(default_factory=dict)
    weight_config_version: str = "1.0"


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------

class ConfidenceIndicator(BaseModel):
    """EVAL-CONFIDENCE — how much data underpins the score."""
    level: ConfidenceLevel
    measures_available: int
    measures_total: int
    completeness_ratio: float = Field(..., ge=0.0, le=1.0)
    missing_measures: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Subscore / measure result
# ---------------------------------------------------------------------------

class MeasureResult(BaseModel):
    """Scored result for one measure — sits inside the response payload."""
    name: str
    raw_value: Optional[Any] = None
    normalized_score: Optional[float] = None
    weight: float
    weighted_contribution: Optional[float] = None
    normalization_method: Optional[NormalizationMethod] = None
    missing_data: bool = False
    source: Optional[str] = None


# ---------------------------------------------------------------------------
# Scoring response (output)
# ---------------------------------------------------------------------------

class ScoringResponse(BaseModel):
    """Top-level output — composite score + subscores + trace + provenance."""
    entity_id: str
    entity_type: EntityType
    composite_score: Optional[float] = Field(None, ge=0.0, le=1.0)
    measures: List[MeasureResult] = Field(default_factory=list)
    trace: List[MeasureTrace] = Field(default_factory=list)
    confidence: Optional[ConfidenceIndicator] = None
    provenance: Optional[DataProvenance] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    scored_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


# ---------------------------------------------------------------------------
# Explanation
# ---------------------------------------------------------------------------

class ScoreExplanation(BaseModel):
    """EVAL-EXPLAIN — human-readable breakdown of a score."""
    entity_id: str
    entity_type: EntityType
    composite_score: Optional[float] = None
    summary: str
    measure_explanations: List[Dict[str, Any]] = Field(default_factory=list)
    confidence: Optional[ConfidenceIndicator] = None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

class CacheKey(BaseModel):
    """EVAL-CACHE — deterministic key for caching scored results."""
    key: str
    entity_id: str
    entity_type: EntityType

    @staticmethod
    def build(request: ScoringRequest) -> "CacheKey":
        payload = request.model_dump_json(exclude_none=False)
        digest = hashlib.sha256(payload.encode()).hexdigest()
        return CacheKey(
            key=digest,
            entity_id=request.entity_id,
            entity_type=request.entity_type,
        )


class CacheEntry(BaseModel):
    """Stored cache record."""
    key: str
    response: ScoringResponse
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    ttl_seconds: int = 3600
