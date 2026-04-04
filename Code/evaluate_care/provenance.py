# Copyright (c) 2026 Skip Snow. All rights reserved.
# EVAL-DATA-PROVENANCE — data lineage tracking for every score.

from __future__ import annotations

import hashlib

from .models import DataProvenance, MeasureInput, ScoringRequest, WeightConfig


def build_provenance(request: ScoringRequest) -> DataProvenance:
    """Create a DataProvenance record from a scoring request."""
    payload = request.model_dump_json(exclude_none=False)
    input_hash = hashlib.sha256(payload.encode()).hexdigest()

    measure_sources = {
        m.name: m.source for m in request.measures
    }

    return DataProvenance(
        engine_version="0.1.4",
        input_hash=input_hash,
        measure_sources=measure_sources,
        weight_config_version=request.weights.version,
    )
