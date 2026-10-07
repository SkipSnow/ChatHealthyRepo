# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_source_freshness_gate (EPIC-010-F-007) — per-source fetch decision.

Serial, before fetch. The Medicare sources resolve their download URL through
url-discovery (page_url + instructions) rather than a stable env URL, so there
is no cheap HEAD identity to compare against the archive the way the provider
gate does; the correct conservative decision is to fetch each source this run.
The per-source decisions are written to ctx.manifest.metrics['source_freshness']
where medicare_fetch_all_sources reads them. No external calls here.
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    sources = list(getattr(ctx, "orchestrator_sources", None)
                   or ctx.config.get("medicare_sources")
                   or ["medicare_partb", "medicare_partd", "ccsr", "icd10_cm", "openfda_labels"])
    decisions = {s: {"decision": "fetch", "reason": "url-discovery source, fetched each run"}
                 for s in sources}
    ctx.manifest.metrics["source_freshness"] = decisions
    _log.LogPipeline("INFO", "medicare_source_freshness_gate: %s",
                     {k: v["decision"] for k, v in decisions.items()})
    return {"decisions": decisions}


def execute(ctx):
    return run_step(ctx)
