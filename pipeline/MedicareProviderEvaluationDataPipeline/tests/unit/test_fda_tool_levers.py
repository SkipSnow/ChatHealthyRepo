# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The FDA tool's tunable levers are REQUIRED constructor inputs (LLD v83
III.15, H7: no defaults) and the tool carries no external terminology client
(the RxNav usage was an off-design bug, removed 2026-10-20)."""

import inspect

from pipeline.MedicareProviderEvaluationDataPipeline import fda_drug_enrichment_tool


def test_levers_are_required_keyword_only_no_defaults():
    sig = inspect.signature(
        fda_drug_enrichment_tool.FDADrugEnrichmentTool.__init__)
    for lever in ("batch_size", "max_transient_retries", "backoff_base_s"):
        p = sig.parameters[lever]
        assert p.kind is inspect.Parameter.KEYWORD_ONLY
        assert p.default is inspect.Parameter.empty, (
            f"{lever} carries a default; levers come from durable config (H7)")


def test_no_rxnav_client_and_no_http_dependency():
    src = inspect.getsource(fda_drug_enrichment_tool)
    assert "rxnav.nlm.nih.gov" not in src
    assert "import httpx" not in src
    for name in ("resolve_rxcuis", "_run_tier3", "_get_resolution_agent"):
        assert not hasattr(fda_drug_enrichment_tool.FDADrugEnrichmentTool, name)
        assert not hasattr(fda_drug_enrichment_tool, name)
