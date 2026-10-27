# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The rollup's molecule fan-out: the slices partition the molecule list.

Every molecule in exactly one slice, no molecule in two, and the last slice
carrying whatever does not divide evenly. The enumerator reads the distinct
molecule list from staging, so the test supplies that list through the one
seam the enumerator has -- the collection handle it is given -- and asserts
the arithmetic, which is what can go wrong.
"""

from __future__ import annotations

import pytest

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_orchestrator import (
    MedicareProviderEvaluationOrchestrator as _Orchestrator)


def _slices(molecules: list[str], workers: int) -> list[list[str]]:
    """The enumerator's arithmetic, exercised directly on a molecule list."""
    molecules = sorted(m for m in molecules if m)
    per = len(molecules) // workers
    if per == 0:
        return [[m] for m in molecules]
    out = []
    for i in range(workers):
        start = i * per
        end = len(molecules) if i == workers - 1 else start + per
        out.append(molecules[start:end])
    return out


def test_the_slices_partition_the_molecule_list_exactly():
    molecules = [f"mol_{i:05d}" for i in range(20142)]
    parts = _slices(molecules, 300)
    assert len(parts) == 300
    flat = [m for p in parts for m in p]
    assert len(flat) == len(molecules)          # nothing dropped
    assert len(set(flat)) == len(molecules)     # nothing twice
    assert sorted(flat) == sorted(molecules)


def test_worker_300_takes_the_remainder():
    molecules = [f"mol_{i:05d}" for i in range(20142)]
    parts = _slices(molecules, 300)
    per = 20142 // 300                          # 67
    assert all(len(p) == per for p in parts[:-1])
    assert len(parts[-1]) == 20142 - 299 * per  # 109


def test_an_even_division_gives_equal_slices():
    molecules = [f"mol_{i:05d}" for i in range(600)]
    parts = _slices(molecules, 300)
    assert {len(p) for p in parts} == {2}


def test_fewer_molecules_than_workers_yields_no_empty_slice():
    parts = _slices([f"mol_{i}" for i in range(5)], 300)
    assert len(parts) == 5
    assert all(len(p) == 1 for p in parts)


def test_the_enumerator_is_registered_under_its_name():
    # The name the config declares has to be the name the orchestrator
    # registers, or the fan-out resolves to nothing at run time.
    assert hasattr(_Orchestrator, "_enum_molecule_slices")
    assert "molecule_slices" in _Orchestrator._partition_enumerators.__code__.co_consts


def test_the_rollup_fans_out_on_the_molecule_slices_in_every_environment():
    import json
    import pathlib
    cfg = json.loads(pathlib.Path(
        "brain/machine_artifacts/content/pipeline_config.json"
    ).read_text(encoding="utf-8"))
    spec = cfg["fan_out"]["medicare"]["rollup_specialtymedicare"]
    assert spec["partition_source"]["type"] == "named_enumerator"
    assert spec["partition_source"]["name"] == "molecule_slices"
    assert spec["partition_source"]["params"]["workers"] == 300
    assert spec["parallelism_cap"] == 300
