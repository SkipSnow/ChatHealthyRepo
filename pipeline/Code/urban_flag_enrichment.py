# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""urban_flag_enrichment — Step 11 sub-orchestration wrapping the existing
USDA RUCC urban-flag stamper (urban_flag.py).

Per-county derivation, stamped onto practice_address[*].county.urban for every
in-scope element. The underlying stamper traverses providers state-by-state in
a single activity; this sub-orch exists so the step is autonomous from the
prior step in the parent pipeline and gets its own per-activity budget.

Input shape:
    {
        "blob_container":      "...",
        "states":              [...],
        "provider_collection": "...",
        "bulk_batch_size":     <int>,
    }

Output: the activity's return value (a dict with `summary`).
"""

from __future__ import annotations

import logging


def urban_flag_enrichment_orchestrator_fn(context):
    cfg = context.get_input() or {}

    context.set_custom_status("Step 11: Stamping urban flag from USDA RUCC")
    result = yield context.call_activity("stamp_urban_flag_activity", cfg)

    logging.info("urban_flag_enrichment: %s", result)
    return result
