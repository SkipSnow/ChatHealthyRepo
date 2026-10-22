# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""START AT capability -- one file, every step, every pipeline (operator
design 2026-10-22).

The capability under test: a run begins its DAG walk at any formal step named
by start_at_step; before EVERY step the infrastructure calls
StepDataDependenciesAreMet(step_name) on the pipeline child class; the
collections a step consumes either exist with rows (go) or the run dies
fatally. The parent-class function is virtual: a child that fails to override
it dies with not_implemented before any step executes. No history, no
prior-run reference, no idempotency bookkeeping.

Covered here, against the production classes:
  - the start-at slice and its validation, for every medicare and provider step
  - the universal gate: called before every step (finally block included),
    False is fatal, the gated step never executes
  - in-run prerequisite ordering under a start-at slice
  - the medicare per-step dependency declaration (complete, known kinds)
  - the provider pipeline's current contract: no override -> loud fatal on
    every step (its dependency map is parked until Provider reintegration)
  - the argument plumbing: CLI -> namespace -> PipelineArgs

What is NOT tested here: the live collection count_documents checks -- a Mongo
handle is acquired only through ChatHealthyMongoUtilities (Rule-065 stmt 3: no
fakes, mocks or in-memory doubles may stand in for a client, database or
collection), so the data-present/data-absent answers of the real registry
reads are proven by the live Start At fire, not simulated here.
"""

from __future__ import annotations

import pytest

from chathealthy_lib.exceptions import ChatHealthyException

from pipeline.MedicareProviderEvaluationDataPipeline.control_runner import _parse_args
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_orchestrator import (
    MedicareProviderEvaluationOrchestrator)
from pipeline.provider_pipeline.provider_pipeline_orchestrator import (
    ProviderPipelineOrchestrator)
from pipeline.run_lifecycle.base_pipeline_orchestrator import BasePipelineOrchestrator
from pipeline.run_lifecycle.step_context import PipelineArgs

MED_MAIN_STEPS = [s.name for s in MedicareProviderEvaluationOrchestrator.STEPS
                  if s.invocation_phase == "main_loop"]
MED_FINALLY_STEPS = [s.name for s in MedicareProviderEvaluationOrchestrator.STEPS
                     if s.invocation_phase == "finally_block"]
MED_ALL_STEPS = [s.name for s in MedicareProviderEvaluationOrchestrator.STEPS]

PROV_MAIN_STEPS = [s.name for s in ProviderPipelineOrchestrator.STEPS
                   if s.invocation_phase == "main_loop"]
PROV_ALL_STEPS = [s.name for s in ProviderPipelineOrchestrator.STEPS]

# Medicare steps that consume no collection: their dependency answer must be
# True without touching any data resource at all.
MED_NO_DATA_STEPS = [name for name in MED_ALL_STEPS
                     if MedicareProviderEvaluationOrchestrator
                     .STEP_DATA_DEPENDENCIES.get(name) == []]

# The dependency kinds the medicare checker implements, and the conditional
# prefixes it strips. A map entry outside this vocabulary would raise
# not_implemented at run time; this file refuses it at commit time.
KNOWN_KINDS = ("transient", "fda_published", "masters",
               "specialty_published", "provider_spine",
               "fda_stamped", "partd_stamped")
CONDITIONAL_PREFIXES = ("if_build_fda:", "if_consume_fda:")

# Infrastructure preamble: every step up to the first business activity
# (fetch) runs on EVERY run regardless of start point (operator ruling
# 2026-10-22) -- a canceled predecessor leaves the cluster paused and
# partial state behind, and these steps own readiness.
MED_ALWAYS_RUN = [s.name for s in MedicareProviderEvaluationOrchestrator.STEPS
                  if s.always_run]
PROV_ALWAYS_RUN = [s.name for s in ProviderPipelineOrchestrator.STEPS
                   if s.always_run]


def _expected_walk(start: str) -> list[str]:
    """The scheduled main steps for a medicare start point: the always-run
    preamble (those before the start point) plus the tail from the start."""
    idx = MED_MAIN_STEPS.index(start)
    return [n for i, n in enumerate(MED_MAIN_STEPS)
            if n in MED_ALWAYS_RUN or i >= idx]


def _med_args(**overrides) -> PipelineArgs:
    base = dict(states=["DE", "MT"], env_prefix="dev", data_version=1,
                provider_collection=(
                    "ChatHealthyDataPipelines.PipelinePublicHealthData.Provider_v_5"))
    base.update(overrides)
    return PipelineArgs(**base)


def _prov_args(**overrides) -> PipelineArgs:
    base = dict(states=["VT", "DE"], env_prefix="dev", data_version=1)
    base.update(overrides)
    return PipelineArgs(**base)


class _MedicareWalkRecorder(MedicareProviderEvaluationOrchestrator):
    """The production medicare orchestrator with the two boundaries that need
    external resources replaced by recorders: step execution (workers, Azure)
    and the data gate's collection reads (Mongo). The DAG walk, the start-at
    slice, the gate call sequence and the prerequisite enforcement under test
    are the REAL base-class code paths."""

    def __init__(self, *, gate_answers=None):
        super().__init__(env="dev", config={}, mongo_client=None, blob_client=None)
        self.invoked: list[str] = []
        self.gated: list[str] = []
        self._gate_answers = gate_answers or {}

    def StepDataDependenciesAreMet(self, step_name, ctx):
        self.gated.append(step_name)
        return self._gate_answers.get(step_name, True)

    def _invoke_step(self, spec, ctx):
        self.invoked.append(spec.name)
        ctx.manifest.completed_steps.add(spec.name)


class _ProviderWalkRecorder(ProviderPipelineOrchestrator):
    """The production provider orchestrator with step execution replaced by a
    recorder. StepDataDependenciesAreMet is deliberately NOT overridden -- the
    virtual contract under test is the real one."""

    def __init__(self):
        super().__init__(env="dev", config={}, mongo_client=None, blob_client=None)
        self.invoked: list[str] = []

    def _invoke_step(self, spec, ctx):
        self.invoked.append(spec.name)
        ctx.manifest.completed_steps.add(spec.name)


# ==========================================================================
# MEDICARE -- POSITIVE: the slice; every formal step is a legal entry point
# ==========================================================================

@pytest.mark.parametrize("step_name", MED_MAIN_STEPS)
def test_medicare_start_at_every_step_walks_preamble_plus_tail(step_name):
    orch = _MedicareWalkRecorder()
    manifest = orch.run(_med_args(start_at_step=step_name))
    assert orch.invoked == _expected_walk(step_name) + MED_FINALLY_STEPS
    assert manifest.status == "succeeded"


@pytest.mark.parametrize("step_name", MED_MAIN_STEPS)
def test_medicare_infrastructure_preamble_runs_on_every_start_point(step_name):
    # A canceled predecessor leaves the cluster paused; prepare and the
    # freshness gate own readiness and may NEVER be sliced away.
    orch = _MedicareWalkRecorder()
    orch.run(_med_args(start_at_step=step_name))
    for always in MED_ALWAYS_RUN:
        assert always in orch.invoked, (
            f"start_at={step_name} skipped infrastructure step {always}")
    assert orch.invoked[:len(MED_ALWAYS_RUN)] == MED_ALWAYS_RUN or (
        MED_MAIN_STEPS.index(step_name) <= MED_MAIN_STEPS.index(MED_ALWAYS_RUN[-1])), (
        "the always-run preamble must execute first, in declared order")


def test_medicare_always_run_is_everything_before_the_first_business_activity():
    # Operator ruling: fetch is the first business activity; every step
    # before it is infrastructure and always runs.
    fetch_idx = MED_MAIN_STEPS.index("medicare_fetch_all_sources")
    assert MED_ALWAYS_RUN == MED_MAIN_STEPS[:fetch_idx]


def test_medicare_no_start_at_walks_every_step_in_declared_order():
    orch = _MedicareWalkRecorder()
    manifest = orch.run(_med_args())
    assert orch.invoked == MED_MAIN_STEPS + MED_FINALLY_STEPS
    assert manifest.status == "succeeded"


def test_medicare_start_at_first_step_is_identical_to_a_full_run():
    full = _MedicareWalkRecorder()
    full.run(_med_args())
    sliced = _MedicareWalkRecorder()
    sliced.run(_med_args(start_at_step=MED_MAIN_STEPS[0]))
    assert sliced.invoked == full.invoked


def test_medicare_gate_is_called_before_every_step_including_finally():
    orch = _MedicareWalkRecorder()
    orch.run(_med_args())
    assert orch.gated == MED_MAIN_STEPS + MED_FINALLY_STEPS


def test_medicare_gate_is_called_only_for_scheduled_steps_on_start_at():
    start = "prepare_molecule_masters"
    orch = _MedicareWalkRecorder()
    orch.run(_med_args(start_at_step=start))
    assert orch.gated == _expected_walk(start) + MED_FINALLY_STEPS, (
        "a skipped step must not be gated: the run has no business reading "
        "data for steps it will never execute")


def test_medicare_prereq_before_start_point_is_vouched_by_data_not_history():
    # enrich_drugs requires build_providermedicare_base; starting AT
    # enrich_drugs must pass ordering because the data gate answers for the
    # earlier step's output -- completed_steps carries no history of it.
    orch = _MedicareWalkRecorder()
    manifest = orch.run(_med_args(start_at_step="enrich_drugs"))
    assert manifest.status == "succeeded"
    assert "build_providermedicare_base" not in manifest.completed_steps
    assert orch.invoked == MED_ALWAYS_RUN + ["enrich_drugs",
                                            "enrich_procedures",
                                            "enrich_org_ccn_size",
                                            "rollup_specialtymedicare",
                                            "medicare_contract_conformance"] \
        + MED_FINALLY_STEPS


# ==========================================================================
# MEDICARE -- NEGATIVE: validation, the gate, in-run ordering
# ==========================================================================

def test_medicare_unknown_start_at_step_is_fatal_and_invokes_nothing():
    orch = _MedicareWalkRecorder()
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_med_args(start_at_step="no_such_step"))
    assert exc_info.value.mode == "value_error"
    assert "no_such_step" in exc_info.value.message
    for name in MED_MAIN_STEPS:
        assert name in exc_info.value.message, (
            "the fatal must enumerate every legal step name")
    assert orch.invoked == [], (
        "a bad start_at is rejected before the run begins -- nothing was "
        "provisioned, so not even the finally block runs")


def test_medicare_start_at_the_finally_step_is_rejected():
    orch = _MedicareWalkRecorder()
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_med_args(start_at_step="quiesce_infrastructure"))
    assert exc_info.value.mode == "value_error"


def test_medicare_gate_false_kills_the_run_before_the_step_executes():
    start = "index_fda_prescription_records"
    orch = _MedicareWalkRecorder(gate_answers={start: False})
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_med_args(start_at_step=start))
    assert exc_info.value.mode == "pipeline_data_dependencies_unmet"
    assert start in exc_info.value.message
    assert orch.invoked == MED_ALWAYS_RUN + MED_FINALLY_STEPS, (
        "the infrastructure preamble runs, the gated step never executes, "
        "and the finally block quiesces what was provisioned")


def test_medicare_gate_false_mid_walk_kills_the_run_at_that_step():
    victim = "finalize_fda_molecules"
    orch = _MedicareWalkRecorder(gate_answers={victim: False})
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_med_args(start_at_step="index_fda_prescription_records"))
    assert exc_info.value.mode == "pipeline_data_dependencies_unmet"
    assert orch.invoked == MED_ALWAYS_RUN + [
        "index_fda_prescription_records",
        "build_fda_label_resolution"] + MED_FINALLY_STEPS
    assert victim not in orch.invoked


def test_medicare_gate_false_on_the_finally_step_blocks_it_not_the_run():
    orch = _MedicareWalkRecorder(gate_answers={"quiesce_infrastructure": False})
    manifest = orch.run(_med_args())
    assert manifest.status == "succeeded"
    assert "quiesce_infrastructure" not in orch.invoked


def test_medicare_prereq_inside_the_scheduled_window_is_still_enforced():
    class _DropsCompletion(_MedicareWalkRecorder):
        def _invoke_step(self, spec, ctx):
            self.invoked.append(spec.name)
            if spec.name != "index_fda_prescription_records":
                ctx.manifest.completed_steps.add(spec.name)

    orch = _DropsCompletion()
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_med_args(start_at_step="index_fda_prescription_records"))
    assert exc_info.value.mode == "runtime_error"
    assert "build_fda_label_resolution" in exc_info.value.message


# ==========================================================================
# MEDICARE -- the declaration: every step answers, nothing extra, known kinds
# ==========================================================================

def test_medicare_every_step_declares_its_data_dependencies():
    declared = set(MedicareProviderEvaluationOrchestrator.STEP_DATA_DEPENDENCIES)
    steps = set(MED_ALL_STEPS)
    assert declared == steps, (
        f"undeclared steps: {sorted(steps - declared)}; "
        f"orphan declarations: {sorted(declared - steps)}")


def test_medicare_every_declared_dependency_is_a_known_kind():
    for step, deps in (MedicareProviderEvaluationOrchestrator
                       .STEP_DATA_DEPENDENCIES.items()):
        for dep in deps:
            for prefix in CONDITIONAL_PREFIXES:
                if dep.startswith(prefix):
                    dep = dep[len(prefix):]
                    break
            assert dep.startswith("staging:") or dep in KNOWN_KINDS, (
                f"step {step}: dependency {dep!r} names no kind the checker "
                f"implements -- it would raise not_implemented at run time")


@pytest.mark.parametrize("step_name", MED_NO_DATA_STEPS)
def test_medicare_no_data_steps_answer_true_without_resources(step_name):
    orch = object.__new__(MedicareProviderEvaluationOrchestrator)
    assert orch.StepDataDependenciesAreMet(step_name, None) is True


def test_medicare_no_data_steps_are_exactly_infra_fetch_load_and_quiesce():
    assert MED_NO_DATA_STEPS == ["prepare_infrastructure",
                                 "medicare_source_freshness_gate",
                                 "medicare_fetch_all_sources",
                                 "medicare_source_archival",
                                 "medicare_load_staging",
                                 "quiesce_infrastructure"]


def test_medicare_unknown_step_name_raises_not_implemented_never_passes():
    orch = object.__new__(MedicareProviderEvaluationOrchestrator)
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.StepDataDependenciesAreMet("no_such_step", None)
    assert exc_info.value.mode == "not_implemented"
    assert "no_such_step" in exc_info.value.message


# ==========================================================================
# PROVIDER -- the virtual contract: every step, no override, loud fatal
# ==========================================================================

def test_provider_does_not_override_the_gate():
    assert ("StepDataDependenciesAreMet"
            not in ProviderPipelineOrchestrator.__dict__), (
        "the provider pipeline now overrides StepDataDependenciesAreMet -- "
        "replace this section's raising tests with declaration tests like "
        "the medicare section's")


@pytest.mark.parametrize("step_name", PROV_ALL_STEPS)
def test_provider_every_step_raises_not_implemented_without_override(step_name):
    orch = object.__new__(ProviderPipelineOrchestrator)
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.StepDataDependenciesAreMet(step_name, None)
    assert exc_info.value.mode == "not_implemented"
    assert step_name in exc_info.value.message
    assert "ProviderPipelineOrchestrator" in exc_info.value.message, (
        "the fatal must name the class that failed to declare")


def test_provider_full_run_dies_at_the_first_gate_before_any_step():
    orch = _ProviderWalkRecorder()
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_prov_args())
    assert exc_info.value.mode == "not_implemented"
    assert orch.invoked == [], (
        "no step may execute while the pipeline cannot answer for its data "
        "(quiesce is also gated, so even the finally block stays shut)")


@pytest.mark.parametrize("step_name", PROV_MAIN_STEPS)
def test_provider_start_at_any_step_dies_at_the_first_gate(step_name):
    orch = _ProviderWalkRecorder()
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_prov_args(start_at_step=step_name))
    assert exc_info.value.mode == "not_implemented"
    first_scheduled = (PROV_ALWAYS_RUN[0] if PROV_ALWAYS_RUN else step_name)
    assert first_scheduled in exc_info.value.message, (
        "the gate must be asked about the first scheduled step -- the "
        "infrastructure preamble -- before anything executes")
    assert orch.invoked == []


def test_provider_unknown_start_at_step_is_rejected_before_the_gate():
    orch = _ProviderWalkRecorder()
    with pytest.raises(ChatHealthyException) as exc_info:
        orch.run(_prov_args(start_at_step="no_such_step"))
    assert exc_info.value.mode == "value_error"
    assert "no_such_step" in exc_info.value.message
    for name in PROV_MAIN_STEPS:
        assert name in exc_info.value.message, (
            "the fatal must enumerate every legal step name")


# ==========================================================================
# BASE CLASS -- the virtual function itself
# ==========================================================================

def test_base_class_gate_is_virtual_and_raises_unimplemented():
    with pytest.raises(ChatHealthyException) as exc_info:
        BasePipelineOrchestrator.StepDataDependenciesAreMet(
            object.__new__(BasePipelineOrchestrator), "any_step", None)
    assert exc_info.value.mode == "not_implemented"


# ==========================================================================
# ARGUMENT PLUMBING -- CLI -> namespace -> PipelineArgs
# ==========================================================================

_REQUIRED_CLI = ["--data-version", "1", "--run-id", "test-run",
                 "--provider-collection", "c.d.coll"]


def test_cli_start_at_step_reaches_the_namespace(monkeypatch):
    monkeypatch.delenv("START_AT_STEP", raising=False)
    monkeypatch.delenv("DATA_VERSION", raising=False)
    ns = _parse_args(_REQUIRED_CLI
                     + ["--start-at-step", "index_fda_prescription_records"])
    assert ns.start_at_step == "index_fda_prescription_records"


def test_env_start_at_step_is_the_fallback(monkeypatch):
    monkeypatch.setenv("START_AT_STEP", "prepare_molecule_masters")
    monkeypatch.delenv("DATA_VERSION", raising=False)
    ns = _parse_args(_REQUIRED_CLI)
    assert ns.start_at_step == "prepare_molecule_masters"


def test_cli_start_at_step_beats_the_env(monkeypatch):
    monkeypatch.setenv("START_AT_STEP", "prepare_molecule_masters")
    monkeypatch.delenv("DATA_VERSION", raising=False)
    ns = _parse_args(_REQUIRED_CLI + ["--start-at-step", "enrich_drugs"])
    assert ns.start_at_step == "enrich_drugs"


def test_absent_start_at_step_is_none_meaning_full_run(monkeypatch):
    monkeypatch.delenv("START_AT_STEP", raising=False)
    monkeypatch.delenv("DATA_VERSION", raising=False)
    ns = _parse_args(_REQUIRED_CLI)
    assert ns.start_at_step is None


def test_pipeline_args_carries_start_at_step_and_defaults_to_none():
    assert _med_args().start_at_step is None
    assert (_med_args(start_at_step="enrich_drugs").start_at_step
            == "enrich_drugs")
