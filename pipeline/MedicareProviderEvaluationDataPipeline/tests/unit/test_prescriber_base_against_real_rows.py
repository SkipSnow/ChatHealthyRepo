# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""CMS_NPI_PrescriberBase against REAL CMS rows.

The test bed is verbatim staged Part D rows pulled from
ChatHealthyDataPipelines.PublicStaging.StagingMedicarePartD_v_1 on 2026-10-23
and committed as fixtures: every row for the top three molecules' busiest
prescribers -- Levothyroxine Sodium (7 brands for one prescriber), Metformin
Hcl (5 brands), Atorvastatin Calcium (2 brands). These are the real multi-row
shapes the fold exists for; 4.34% of the 26.7M records look like this.

What is proven here:
  * the fold's arithmetic against hand-recomputed totals from those rows
  * that every source value is a string and is coerced before summing
  * the record document the one owning worker writes: complete in a single
    write, carrying its key, its scope key and its drug facts

One writer, one write (operator ruling 2026-10-24). A record's rows all carry
one CMS state, so the worker holding that state holds every row of that
record: there is no merge to prove and no replay to reconcile. This is an ETL
process -- a partition that dies fails the run and the run is re-driven from
the clear, against an empty collection.

The live write itself is proven by a run, not here -- Rule-065 forbids
standing a fake in place of a Mongo collection, so these tests exercise the
pure functions and the documents they produce.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    fold_rows, record_document, record_id)

C = PARTD_COLS
from pipeline.MedicareProviderEvaluationDataPipeline.tests.fixtures.partd_rows import (
    FIXTURES)


def _fixture(name: str) -> dict:
    return FIXTURES[name]


LEVOTHYROXINE = "fixtures_partd_mol_efcd0ff275b7e613"
METFORMIN = "fixtures_partd_mol_63c35c75665f7dfc"
ATORVASTATIN = "fixtures_partd_mol_88c61dc3cfb05e59"


def _rows_for(fixture_name: str, npi: str) -> list[dict]:
    data = _fixture(fixture_name)
    return [r for r in data["rows"] if str(r.get(C["npi"])) == str(npi)]


def _npis(fixture_name: str) -> list[str]:
    return _fixture(fixture_name)["npis"]


def _expected(rows: list[dict]) -> dict:
    """Recompute the facts independently of the engine, straight off the raw
    strings, the way an auditor would."""
    brands, generics = [], []
    clms = suply = 0
    for r in rows:
        b = (r.get(C["brand"]) or "").strip()
        if b and b not in brands:
            brands.append(b)
        g = (r.get(C["generic"]) or "").strip()
        if g and g not in generics:
            generics.append(g)
        clms += int(float(r[C["total_claims"]]))
        suply += int(float(r[C["days_supply"]]))
    return {"brand_names": brands, "generic_names": generics,
            "tot_clms": clms, "tot_day_suply": suply,
            "record_count": len(rows)}


# ------------------------------------------------- the real fixture shape

def test_the_test_bed_is_real_cms_rows_with_string_values():
    rows = _fixture(LEVOTHYROXINE)["rows"]
    assert rows, "fixture is empty"
    sample = rows[0]
    assert C["npi"] in sample and C["brand"] in sample
    assert isinstance(sample[C["total_claims"]], str), (
        "the CMS source carries quantities as strings; a fixture of ints "
        "would not test the coercion")


def test_the_multi_brand_shape_the_fold_exists_for():
    npi = _npis(LEVOTHYROXINE)[0]
    rows = _rows_for(LEVOTHYROXINE, npi)
    brands = {(r.get(C["brand"]) or "").strip() for r in rows}
    assert len(rows) > 1 and len(brands) > 1, (
        "this prescriber should carry several brands of one molecule")


# ------------------------------------------- the fold against real totals

@pytest.mark.parametrize("fixture_name", [LEVOTHYROXINE, METFORMIN, ATORVASTATIN])
def test_fold_matches_independently_recomputed_totals(fixture_name):
    for npi in _npis(fixture_name):
        rows = _rows_for(fixture_name, npi)
        assert fold_rows(rows) == _expected(rows), (
            f"fold disagrees with recomputation for npi={npi}")


def test_fold_sums_the_seven_brand_levothyroxine_prescriber():
    npi = _npis(LEVOTHYROXINE)[0]
    rows = _rows_for(LEVOTHYROXINE, npi)
    facts = fold_rows(rows)
    hand_total = sum(int(float(r[C["total_claims"]])) for r in rows)
    assert facts["tot_clms"] == hand_total
    assert facts["record_count"] == len(rows)
    assert len(facts["brand_names"]) == len(
        {(r.get(C["brand"]) or "").strip() for r in rows})


def test_quantities_are_integers_not_strings_after_the_fold():
    rows = _rows_for(METFORMIN, _npis(METFORMIN)[0])
    facts = fold_rows(rows)
    assert isinstance(facts["tot_clms"], int)
    assert isinstance(facts["tot_day_suply"], int)


def test_fold_of_a_single_row_is_that_row():
    rows = _rows_for(ATORVASTATIN, _npis(ATORVASTATIN)[0])[:1]
    facts = fold_rows(rows)
    assert facts["record_count"] == 1
    assert facts["tot_clms"] == int(float(rows[0][C["total_claims"]]))


# ------------------------------------------ the write: one complete record

def _document_for(fixture_name: str, npi: str, state: str = "DE"):
    rows = _rows_for(fixture_name, npi)
    mid = _fixture(fixture_name)["molecule_id"]
    return record_document(npi, mid, fold_rows(rows), state), rows, mid


def test_the_document_is_keyed_by_npi_and_molecule():
    doc, _, mid = _document_for(LEVOTHYROXINE, _npis(LEVOTHYROXINE)[0])
    assert doc["_id"] == record_id(_npis(LEVOTHYROXINE)[0], mid)
    assert doc["npi"] == _npis(LEVOTHYROXINE)[0]
    assert doc["molecule_id"] == mid


def test_the_document_is_complete_in_one_write():
    doc, rows, _ = _document_for(METFORMIN, _npis(METFORMIN)[0])
    assert doc["record_count"] == len(rows)
    assert doc["tot_clms"] == sum(int(float(r[C["total_claims"]])) for r in rows)
    assert doc["brand_names"] and doc["generic_names"]
    assert doc["medicare_population"] == "Part D including Medicare Advantage"


def test_no_merge_or_replay_scaffolding_survives():
    doc, _, _ = _document_for(ATORVASTATIN, _npis(ATORVASTATIN)[0])
    assert "contributed_by" not in doc, (
        "the per-unit marker is gone: one worker owns every row of a record")
    assert not [k for k in doc if k.startswith("$")]


def test_the_scope_key_is_carried_for_the_later_steps():
    doc, _, _ = _document_for(ATORVASTATIN, _npis(ATORVASTATIN)[0], state="MT")
    assert doc["source_state"] == "MT"


def test_no_beneficiary_count_reaches_the_record():
    doc, _, _ = _document_for(LEVOTHYROXINE, _npis(LEVOTHYROXINE)[0])
    flat = json.dumps(doc, default=str).lower()
    assert "bene" not in flat
