# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""fetch_all_sources — Provider Pipeline step wrapper for LLD v23 §4.4.

Bridges the orchestrator's StepContext to the pure business-logic module
source_fetch_engine.fetch_all_sources().

Realizes:
  - EPIC-010-F-102-S-003 (data-source fetching, throttled concurrency)
  - EPIC-010-F-103-S-002/003/004/005 (per-source enumeration)
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


from source_fetch_engine import fetch_all_sources

_log = ChatHealthyLoggingService()


def _default_source_specs(env_prefix: str) -> dict:
    """Provide the LLD v23 §4.4 source inventory when ctx.config omits it."""
    return {
        "nppes_npi": {
            "url_discovery": {
                "page_url": "https://download.cms.gov/nppes/NPI_Files.html",
                "instructions": (
                    "Find the current monthly NPPES full dissemination ZIP "
                    "(pattern NPPES_Data_Dissemination_MonthYear.zip). "
                    "Return the direct download URL."
                ),
            },
        },
        "pl_pfile": {
            "derived_from": "nppes_npi",
            "zip_entry_glob": "pl_pfile*.csv",
        },
        "nucc": {
            "url_discovery": {
                "page_url": "https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57",
                "instructions": (
                    "Find the current NUCC provider taxonomy CSV file "
                    "download URL."
                ),
            },
        },
        "census_zcta_county": {
            "source_url": "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt",
        },
        "usda_rucc": {
            "url_discovery": {
                "page_url": "https://www.ers.usda.gov/data-products/rural-urban-continuum-codes/",
                "instructions": (
                    "Find the current USDA Rural-Urban Continuum Codes "
                    "workbook (xlsx) download URL."
                ),
            },
        },
    }


def run_step(ctx) -> dict:
    config = dict(ctx.config)
    config.setdefault("run_id", ctx.run_id)
    config.setdefault("env", ctx.env_prefix)
    config.setdefault(
        "transient_container", f"{ctx.env_prefix}-pipeline-transients"
    )

    freshness = ctx.manifest.metrics.get("source_freshness") or {}
    sources = config.get("sources") or _default_source_specs(ctx.env_prefix)
    for name, spec in sources.items():
        spec.setdefault("freshness_decision", freshness.get(name, "fetch"))
    config["sources"] = sources

    result = fetch_all_sources(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    ctx.manifest.source_versions.update(result.get("source_versions") or {})
    ctx.manifest.metrics["fetch_results"] = {
        r["source_name"]: r for r in result.get("results") or []
    }
    return result


def execute(ctx):
    return run_step(ctx)
