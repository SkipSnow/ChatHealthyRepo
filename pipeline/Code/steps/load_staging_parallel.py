# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""load_staging_parallel — Provider Pipeline step wrapper for LLD v23 §4.6.

Loads every fetched source into its per-source staging collection on the
pipeline cluster (byte-offset-indexed; NPI-keyed for secondary sources).
NPPES NPI is serial single-PID; the other sources load in parallel.
"""

from __future__ import annotations

import logging

from staging_loader import STAGING_COLLECTIONS, load_staging

_log = logging.getLogger(__name__)


_FORMAT_BY_SOURCE = {
    "nppes_npi": ("zip_csv", "npidata"),
    "pl_pfile": ("zip_csv", "pl_pfile"),
    "nucc": ("csv", None),
    "census_zcta_county": ("csv", None),
    "usda_rucc": ("csv", None),
    "specialty_catalog": ("json", None),
}


def _build_source_specs(fetch_results: dict) -> dict:
    """Convert fetch_results (LLD §4.4) into staging_loader spec input."""
    sources: dict = {}
    for name, spec in _FORMAT_BY_SOURCE.items():
        r = fetch_results.get(name)
        if not r or r.get("skipped"):
            continue
        fmt, hint = spec
        entry = {
            "blob_container": r.get("blob_container"),
            "blob_path": r.get("blob_path"),
            "format": fmt,
        }
        if hint:
            entry["inner_name_hint"] = hint
        sources[name] = entry
    return sources


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)

    fetch_results = ctx.manifest.metrics.get("fetch_results") or {}
    sources = config.get("sources") or _build_source_specs(fetch_results)
    if not sources:
        _log.warning("load_staging_parallel: no fetched sources to load")
        return {"results": [], "total_inserted": 0}
    config["sources"] = sources

    result = load_staging(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    ctx.manifest.metrics["staging_load"] = {
        "total_inserted": result.get("total_inserted"),
        "per_source": {r.get("source_name"): r for r in result.get("results") or []},
        "collections": {n: STAGING_COLLECTIONS[n] for n in sources.keys() if n in STAGING_COLLECTIONS},
    }
    return result


def execute(ctx):
    return run_step(ctx)
