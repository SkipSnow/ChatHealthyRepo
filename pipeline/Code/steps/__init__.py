# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Provider Pipeline LLD v23 — step-runner registry.

Populated at import time by walking every module under this package and
resolving a callable per LLD-v23 step name. Preferred surface is
run_step(ctx); execute(ctx) is accepted as an equivalent surface on
steps that have not yet been renamed. The alias table below maps the
LLD-v23 canonical step name to the module file when the two differ.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import importlib

import pkgutil
from types import ModuleType
from typing import Callable

_log = ChatHealthyLoggingService()


# LLD v23 §3.2 step name -> module name in this package when they differ.
# Steps whose module name equals the LLD name do not appear here.
_MODULE_ALIASES: dict[str, str] = {
    "source_archival": "archive_sources",
    "load_f006_catalog": "load_nucc_classification_catalog",
    "add_secondary_practices": "attach_practice_addresses",
    "county_enrichment": "county_enrichment_cascade",
}


# Steps whose implementation is shared by every pipeline and therefore lives
# in chathealthy_lib rather than in this package.
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
    package_name = __name__
    package_path = __path__  # provided by pkgutil for packages

    module_runners: dict[str, Callable] = {}
    for info in pkgutil.iter_modules(package_path):
        if info.ispkg:
            continue
        mod_name = info.name
        if mod_name.startswith("_"):
            continue
        try:
            module = importlib.import_module(f"{package_name}.{mod_name}")
        except Exception as exc:  # pragma: no cover - surfaced by control loop
            _log.warning("steps registry: failed to import %s: %s", mod_name, exc)
            continue
        runner = _resolve_runner(module)
        if runner is None:
            _log.warning(
                "steps registry: %s exposes neither run_step nor execute", mod_name
            )
            continue
        module_runners[mod_name] = runner

    # First register modules under their own file name so worker_runner
    # can look up either the LLD name or the file name.
    registry.update(module_runners)

    # Then register LLD canonical step names under aliases.
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

    # For modules whose file name already matches the LLD name, the
    # entry from module_runners already covers the LLD lookup.
    return registry


STEP_RUNNERS: dict[str, Callable] = _build_registry()


def get_runner(step_name: str) -> Callable:
    fn = STEP_RUNNERS.get(step_name)
    if fn is None:
        raise ChatHealthyException(mode="key_error", message=f"No runner registered for step {step_name!r}. "
            f"Known steps: {sorted(STEP_RUNNERS.keys())}"
        )
    return fn
