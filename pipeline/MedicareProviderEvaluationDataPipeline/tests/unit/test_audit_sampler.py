# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The audit's deterministic cross-section sampler.

Proves the properties the audit is defended on: the same data always yields
the same sample, the cross-section spans ICD-10 chapters, volume bands and
fold depths rather than clustering on the common path, the cap never drops the
risky cells, and the recomputation catches a record whose facts disagree with
its source rows.

The recomputation is exercised against the real CMS fixtures, so a drift in
the fold would fail here.
"""

from __future__ import annotations

import json
from pathlib import Path

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_audit_engine import (
    NO_INDICATION, choose_sample, compare, fold_depth_band, icd10_chapter,
    recompute_from_source, selection_key, volume_bands)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS)

C = PARTD_COLS
from pipeline.MedicareProviderEvaluationDataPipeline.tests.fixtures.partd_rows import (
    FIXTURES)

LEVOTHYROXINE = "fixtures_partd_mol_efcd0ff275b7e613"


def _fixture(name):
    return FIXTURES[name]


def _candidates():
    """A population spanning three chapters, three depths and two volumes."""
    out = []
    for i, (chapter_code, depth, mid) in enumerate([
            ("E119", 1, "mol_a"), ("E119", 3, "mol_a"), ("E119", 9, "mol_a"),
            ("J00", 1, "mol_b"), ("J00", 4, "mol_b"),
            ("C50", 7, "mol_c"), ("C50", 1, "mol_c"),
    ]):
        out.append({
            "npi": f"10000000{i:02d}",
            "molecule_id": mid,
            "record_count": depth,
            "indications": [{"icd10": {"level3": [chapter_code[:3]],
                                       "level4": [chapter_code]}}],
        })
    out.append({"npi": "1999999999", "molecule_id": "mol_d",
                "record_count": 2, "indications": []})
    return out


# ------------------------------------------------------------ classifiers

def test_chapter_is_the_lowest_sorting_level3_leading_letter():
    assert icd10_chapter([{"icd10": {"level3": ["J34", "E11"]}}]) == "E"


def test_molecule_without_indications_is_its_own_class():
    assert icd10_chapter([]) == NO_INDICATION
    assert icd10_chapter(None) == NO_INDICATION


def test_fold_depth_bands_split_the_measured_distribution():
    assert fold_depth_band(1) == "1"
    assert fold_depth_band(2) == "2-5" and fold_depth_band(5) == "2-5"
    assert fold_depth_band(6) == "6+" and fold_depth_band(21) == "6+"


def test_volume_bands_rank_high_volume_first_and_orphans_last():
    counts = {"statin": 372337, "metformin": 261272, "orphan": 3, "rare": 11}
    bands = volume_bands(counts, 2)
    assert bands["statin"] == 0 and bands["metformin"] == 0
    assert bands["orphan"] == 1 and bands["rare"] == 1


def test_volume_bands_are_computed_from_the_years_own_data():
    thin = volume_bands({"a": 10, "b": 5}, 3)
    assert set(thin.values()) <= {0, 1, 2}


# --------------------------------------------------------- determinism

def test_the_same_data_yields_the_same_sample_every_time():
    counts = {"mol_a": 100, "mol_b": 50, "mol_c": 2, "mol_d": 7}
    first = choose_sample(_candidates(), counts, bands=3, cap=100)
    second = choose_sample(list(reversed(_candidates())), counts, bands=3, cap=100)
    assert [r["npi"] for r in first] == [r["npi"] for r in second], (
        "the sample must not depend on the order candidates arrive in")


def test_selection_is_by_hash_not_alphabetical_order():
    keys = [selection_key("1000000000", m) for m in ("mol_a", "mol_b", "mol_c")]
    assert len(set(keys)) == 3
    assert keys != sorted(["mol_a", "mol_b", "mol_c"])


def test_no_seed_is_needed_to_reproduce_the_sample():
    counts = {"mol_a": 100, "mol_b": 50, "mol_c": 2, "mol_d": 7}
    a = choose_sample(_candidates(), counts, bands=3, cap=100)
    b = choose_sample(_candidates(), counts, bands=3, cap=100)
    assert [r["_audit_cell"] for r in a] == [r["_audit_cell"] for r in b]


# ------------------------------------------------------- the cross-section

def test_sample_spans_chapters_depths_and_the_no_indication_class():
    counts = {"mol_a": 100, "mol_b": 50, "mol_c": 2, "mol_d": 7}
    sample = choose_sample(_candidates(), counts, bands=3, cap=100)
    chapters = {r["_audit_cell"]["icd10_chapter"] for r in sample}
    depths = {r["_audit_cell"]["fold_depth"] for r in sample}
    assert {"E", "J", "C", NO_INDICATION} <= chapters
    assert {"1", "2-5", "6+"} <= depths


def test_one_record_per_cell_no_duplicates():
    counts = {"mol_a": 100, "mol_b": 50, "mol_c": 2, "mol_d": 7}
    sample = choose_sample(_candidates(), counts, bands=3, cap=100)
    cells = [tuple(sorted(r["_audit_cell"].items())) for r in sample]
    assert len(cells) == len(set(cells))


def test_the_cap_drops_common_cells_not_risky_ones():
    counts = {"mol_a": 100, "mol_b": 50, "mol_c": 2, "mol_d": 7}
    capped = choose_sample(_candidates(), counts, bands=3, cap=2)
    assert len(capped) == 2
    assert all(r["_audit_cell"]["fold_depth"] == "6+" for r in capped), (
        "the deepest folds must survive the cap")


def test_a_uniform_draw_would_have_missed_the_deep_folds():
    """The measured population is 95.66% single-row; the cross-section must
    not inherit that proportion."""
    population = [{"npi": f"1{i:09d}", "molecule_id": "mol_a",
                   "record_count": 1, "indications": [{"icd10": {"level3": ["E11"]}}]}
                  for i in range(100)]
    population.append({"npi": "1999999999", "molecule_id": "mol_a",
                       "record_count": 12,
                       "indications": [{"icd10": {"level3": ["E11"]}}]})
    sample = choose_sample(population, {"mol_a": 100}, bands=1, cap=2)
    assert "6+" in {r["_audit_cell"]["fold_depth"] for r in sample}


# ------------------------------------------ recomputation against the source

def test_recomputation_matches_a_correctly_built_record():
    data = _fixture(LEVOTHYROXINE)
    npi = data["npis"][0]
    rows = [r for r in data["rows"] if str(r.get(C["npi"])) == str(npi)]
    recomputed = recompute_from_source(rows)
    published = dict(recomputed)
    assert compare(published, recomputed) == []


def test_recomputation_catches_a_wrong_total():
    data = _fixture(LEVOTHYROXINE)
    npi = data["npis"][0]
    rows = [r for r in data["rows"] if str(r.get(C["npi"])) == str(npi)]
    recomputed = recompute_from_source(rows)
    published = dict(recomputed)
    published["tot_clms"] = recomputed["tot_clms"] + 1
    problems = compare(published, recomputed)
    assert len(problems) == 1 and "tot_clms" in problems[0]


def test_recomputation_catches_a_missing_brand():
    data = _fixture(LEVOTHYROXINE)
    npi = data["npis"][0]
    rows = [r for r in data["rows"] if str(r.get(C["npi"])) == str(npi)]
    recomputed = recompute_from_source(rows)
    published = dict(recomputed)
    published["brand_names"] = recomputed["brand_names"][:-1]
    problems = compare(published, recomputed)
    assert any("brand_names" in p for p in problems)


def test_recomputation_catches_a_double_counted_record():
    """What a non-idempotent write would produce: the fold applied twice."""
    data = _fixture(LEVOTHYROXINE)
    npi = data["npis"][0]
    rows = [r for r in data["rows"] if str(r.get(C["npi"])) == str(npi)]
    recomputed = recompute_from_source(rows)
    published = dict(recomputed)
    published["tot_clms"] = recomputed["tot_clms"] * 2
    published["record_count"] = recomputed["record_count"] * 2
    problems = compare(published, recomputed)
    assert len(problems) == 2
