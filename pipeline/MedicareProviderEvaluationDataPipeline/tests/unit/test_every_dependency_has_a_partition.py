# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""A source a step depends on is a source the run actually fetches and stages.

The run of 2026-10-29 reached build_fda_label_resolution, finished the LLM
crosswalk over 400 ranges, and died at finalize_fda_molecules with "the data
this step consumes is absent or empty". The absent data was StagingRxnormRel.

rxnorm_rel was declared everywhere that describes it -- a dataset_versions
entry naming its UTS release, its zip member rrf/RXNREL.RRF, its file format
and its staging collection; and a data dependency of finalize_fda_molecules
-- and in neither fan-out partition list. So nothing fetched it and nothing
staged it, and because it was never a partition, no partition failed: fetch
reported 9 of 9 and staging 9 of 9, both green, on a nine-source list that
should have held ten.

That is the shape this file exists to catch. Three declarations have to agree
and only two of them were checked against each other by anything:

  * the dataset_versions entry  -- how to fetch and where to stage
  * the fan-out partition lists -- whether a worker is ever assigned to it
  * STEP_DATA_DEPENDENCIES      -- whether a step refuses to run without it

A dependency with no partition is a run that fails after its most expensive
step. A partition with no dataset entry is a worker with nothing to do.
"""

from __future__ import annotations

import json
import pathlib

import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[4]
_CONFIG = _ROOT / "brain" / "machine_artifacts" / "content" / "pipeline_config.json"

_FETCH_STEP = "medicare_fetch_all_sources"
_STAGE_STEP = "medicare_load_staging"


def _config() -> dict:
    return json.loads(_CONFIG.read_text(encoding="utf-8"))


def _partitions(config: dict, step: str) -> list[str]:
    values = (config["fan_out"]["medicare"][step]
              ["partition_source"]["values"])
    return [v["source"] for v in values]


def _dataset_names(config: dict, env: str = "dev") -> set[str]:
    entry = next(e for e in config["pipeline_config"] if e["env"] == env)
    return {d["source_name"] for d in entry["dataset_versions"]}


def _staging_dependencies() -> set[str]:
    """Every source named by a STEP_DATA_DEPENDENCIES entry as staged data.

    The vocabulary is "staging:<source>" and "if_build_fda:staging:<source>";
    the source is the last colon-separated segment.
    """
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_orchestrator import (
        MedicareProviderEvaluationOrchestrator)
    wanted: set[str] = set()
    deps = MedicareProviderEvaluationOrchestrator.STEP_DATA_DEPENDENCIES
    for entries in deps.values():
        for dep in entries or []:
            parts = str(dep).split(":")
            if "staging" in parts:
                wanted.add(parts[-1])
    return wanted


# ── the failure that produced this file ──────────────────────────────────────
def test_every_staged_dependency_is_a_fetch_partition():
    config = _config()
    fetched = set(_partitions(config, _FETCH_STEP))
    missing = sorted(_staging_dependencies() - fetched)
    assert not missing, (
        "these sources are declared as staged data dependencies and are in no "
        f"fetch partition, so nothing downloads them: {missing}")


def test_every_staged_dependency_is_a_staging_partition():
    config = _config()
    staged = set(_partitions(config, _STAGE_STEP))
    missing = sorted(_staging_dependencies() - staged)
    assert not missing, (
        "these sources are declared as staged data dependencies and are in no "
        f"staging partition, so nothing loads them: {missing}")


def test_rxnorm_rel_specifically_is_fetched_and_staged():
    """Named because it is the one that failed, and because the identity
    ladder cannot resolve a brand to its ingredient without RXNREL."""
    config = _config()
    assert "rxnorm_rel" in _partitions(config, _FETCH_STEP)
    assert "rxnorm_rel" in _partitions(config, _STAGE_STEP)


# ── the other two directions of the same disagreement ────────────────────────
def test_fetch_and_staging_fan_out_over_the_same_sources():
    """A source fetched and not staged is a download nobody reads; a source
    staged and not fetched is a worker with no bytes."""
    config = _config()
    fetched = _partitions(config, _FETCH_STEP)
    staged = _partitions(config, _STAGE_STEP)
    assert sorted(fetched) == sorted(staged), (
        f"fetched-not-staged: {sorted(set(fetched) - set(staged))}; "
        f"staged-not-fetched: {sorted(set(staged) - set(fetched))}")


@pytest.mark.parametrize("step", [_FETCH_STEP, _STAGE_STEP])
def test_every_partition_has_a_dataset_entry(step):
    """A partition names a source the configuration knows how to fetch and
    knows where to stage. Without the entry the worker raises at run time."""
    config = _config()
    declared = _dataset_names(config)
    orphans = sorted(set(_partitions(config, step)) - declared)
    assert not orphans, (
        f"{step} fans out over sources with no dataset_versions entry, so a "
        f"worker would claim a partition it cannot resolve: {orphans}")


def test_every_environment_fans_out_identically():
    """The fan-out is declared once for the pipeline, not per environment, so
    a source cannot be staged in dev and absent in prod."""
    config = _config()
    for step in (_FETCH_STEP, _STAGE_STEP):
        values = (config["fan_out"]["medicare"][step]
                  ["partition_source"]["values"])
        names = [v["source"] for v in values]
        assert len(names) == len(set(names)), (
            f"{step} lists a source twice: "
            f"{sorted({n for n in names if names.count(n) > 1})}")
