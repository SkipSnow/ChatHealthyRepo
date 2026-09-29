# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v23 — step-runner registry.

Populated at import time from an explicit table of step modules that now
live across the per-feature packages (run_lifecycle, provider_base,
provider_enrichment, provider_pipeline). Preferred runner surface
is run_step(ctx); execute(ctx) is accepted as an equivalent surface. The
alias table maps the LLD-v23 canonical step name to the module file when the
two differ.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import importlib
from types import ModuleType
from typing import Callable

_log = ChatHealthyLoggingService()


# Step file name -> its dotted import path in the per-feature packages.
# The key is the module file name (what worker_runner/pipeline_worker look up
# when the LLD step name equals the file name); the LLD aliases below cover the
# four steps whose LLD name differs from the module file name.
_STEP_MODULE_PATHS: dict[str, str] = {
    # F-001 run lifecycle
    "prepare_infrastructure": "pipeline.run_lifecycle.steps.prepare_infrastructure",
    "quiesce_infrastructure": "pipeline.run_lifecycle.steps.quiesce_infrastructure",
    # F-003 provider base
    "source_freshness_gate": "pipeline.provider_base.steps.source_freshness_gate",
    "fetch_all_sources": "pipeline.provider_base.steps.fetch_all_sources",
    "archive_sources": "pipeline.provider_base.steps.archive_sources",
    "load_staging_parallel": "pipeline.provider_base.steps.load_staging_parallel",
    "load_nucc_classification_catalog": "pipeline.provider_base.steps.load_nucc_classification_catalog",
    "normalize_nucc": "pipeline.provider_base.steps.normalize_nucc",
    "normalize_npi_per_state_fanout": "pipeline.provider_base.steps.normalize_npi_per_state_fanout",
    # F-004 provider enrichment
    "attach_practice_addresses": "pipeline.provider_enrichment.steps.attach_practice_addresses",
    "provider_flags_enrichment": "pipeline.provider_enrichment.steps.provider_flags_enrichment",
    "harvest_other_identifier_phrases": "pipeline.provider_enrichment.steps.harvest_other_identifier_phrases",
    "classify_other_identifier_phrases": "pipeline.provider_enrichment.steps.classify_other_identifier_phrases",
    "apply_other_identifier_classifications": "pipeline.provider_enrichment.steps.apply_other_identifier_classifications",
    "entity_first_branch": "pipeline.provider_enrichment.steps.entity_first_branch",
    "license_address_repair": "pipeline.provider_enrichment.steps.license_address_repair",
    "county_enrichment_cascade": "pipeline.provider_enrichment.steps.county_enrichment_cascade",
    "urban_flag": "pipeline.provider_enrichment.steps.urban_flag",
    "entity_second_branch": "pipeline.provider_enrichment.steps.entity_second_branch",
    # F-005 provider pipeline (build/publish)
    "publish_smd_and_embed": "pipeline.provider_pipeline.steps.publish_smd_and_embed",
    "publish_provider": "pipeline.provider_pipeline.steps.publish_provider",
    "post_load_reconciliation": "pipeline.provider_pipeline.steps.post_load_reconciliation",
}


# LLD v23 §3.2 step name -> module file name when the two differ.
_MODULE_ALIASES: dict[str, str] = {
    "source_archival": "archive_sources",
    "load_f006_catalog": "load_nucc_classification_catalog",
    "add_secondary_practices": "attach_practice_addresses",
    "county_enrichment": "county_enrichment_cascade",
}


# Steps whose implementation is shared by every pipeline and therefore lives
# in chathealthy_lib rather than in a pipeline package.
_LIB_STEP_MODULES: dict[str, str] = {
    "discrepancy_and_notifications": "chathealthy_lib.discrepancy_report",
}


def _resolve_runner(module: ModuleType) -> Callable | None:
    fn = getattr(module, "run_step", None)
    if callable(fn):
        return fn
    fn = getattr(module, "execute", None)
    if callable(fn):
        return fn
    return None


def _raise_missing_lib_runner(dotted: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="config_error",
        message=f"{dotted} exposes neither run_step nor execute",
        component="steps registry",
    )


def _build_registry() -> dict[str, Callable]:
    registry: dict[str, Callable] = {}

    module_runners: dict[str, Callable] = {}
    for mod_name, dotted in _STEP_MODULE_PATHS.items():
        try:
            module = importlib.import_module(dotted)
        except Exception as exc:  # pragma: no cover - surfaced by control loop
            _log.LogPipeline("WARNING", "steps registry: failed to import %s (%s): %s",
                         mod_name, dotted, exc)
            continue
        runner = _resolve_runner(module)
        if runner is None:
            _log.LogPipeline("WARNING", 
                "steps registry: %s exposes neither run_step nor execute", mod_name
            )
            continue
        module_runners[mod_name] = runner

    # Register modules under their own file name so a lookup by either the
    # LLD name or the file name resolves.
    registry.update(module_runners)

    # Register LLD canonical step names under aliases.
    for lld_name, mod_name in _MODULE_ALIASES.items():
        if mod_name in module_runners:
            registry[lld_name] = module_runners[mod_name]

    for lld_name, dotted in _LIB_STEP_MODULES.items():
        module = importlib.import_module(dotted)
        runner = _resolve_runner(module)
        if runner is None:
            _raise_missing_lib_runner(dotted)
        registry[lld_name] = runner
        registry[dotted.rsplit(".", 1)[-1]] = runner

    return registry


STEP_RUNNERS: dict[str, Callable] = _build_registry()


def get_runner(step_name: str) -> Callable:
    fn = STEP_RUNNERS.get(step_name)
    if fn is None:
        raise ChatHealthyException(mode="key_error", message=f"No runner registered for step {step_name!r}. "
            f"Known steps: {sorted(STEP_RUNNERS.keys())}"
        )
    return fn
