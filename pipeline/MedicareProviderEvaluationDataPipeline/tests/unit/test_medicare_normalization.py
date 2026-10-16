# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for the Medicare normalization/aggregation pure functions
(EPIC-010-F-007).

The CMS Part B / Part D sources are denormalized and joint-keyed; these pure
functions normalize and aggregate them to our grain. The tests run entirely in
memory over fabricated CMS row dicts -- NO Mongo, no mongomock, no stubs (the
functions under test touch no database). They prove the four behaviours the
rewrite turns on:

  1. place-of-service collapse  -> one procedure per (NPI, HCPCS)
  2. brand/generic RXCUI collapse -> one drug per (NPI, RXCUI)
  3. distinct-NPI de-dup         -> one not-found finding per NPI across N rows
  4. the (HCPCS, NUCC) / (RXCUI, NUCC) specialty rollup grain
"""

from __future__ import annotations

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTB_COLS,
    PARTD_COLS,
    aggregate_procedures,
    collect_drugs,
    distinct_billed_npis,
    npis_not_found,
    regrain_drugs_by_rxcui,
    rollup_specialty,
)

B = PARTB_COLS
D = PARTD_COLS


def _partb_row(npi, hcpcs, pos, *, srvcs, benes, submitted, allowed, paid,
               state="CA", descriptor="desc"):
    return {
        B["npi"]: npi,
        B["state"]: state,
        B["hcpcs"]: hcpcs,
        B["descriptor"]: descriptor,
        B["place_of_service"]: pos,
        B["service_count"]: srvcs,
        B["beneficiary_count"]: benes,
        B["submitted_amount"]: submitted,
        B["allowed_amount"]: allowed,
        B["paid_amount"]: paid,
    }


def _partd_row(npi, brand, generic, *, claims, days, benes, cost, state="CA"):
    return {
        D["npi"]: npi,
        D["state"]: state,
        D["brand"]: brand,
        D["generic"]: generic,
        D["total_claims"]: claims,
        D["days_supply"]: days,
        D["beneficiary_count"]: benes,
        D["total_drug_cost"]: cost,
    }


# --- 1. place-of-service collapse -> one procedure per (NPI, HCPCS) ----------

def test_place_of_service_collapse_one_hcpcs_per_npi():
    rows = [
        _partb_row("1000000001", "99213", "F", srvcs=10, benes=8,
                   submitted=200.0, allowed=120.0, paid=96.0),
        _partb_row("1000000001", "99213", "O", srvcs=5, benes=4,
                   submitted=150.0, allowed=90.0, paid=72.0),
    ]
    out = aggregate_procedures(rows)

    assert set(out) == {"1000000001"}
    procs = out["1000000001"]
    # The two place-of-service rows for the same HCPCS collapse to ONE entry.
    assert len(procs) == 1
    p = procs[0]
    assert p["hcpcs"] == "99213"
    # Tot_Srvcs and Tot_Benes are SUMMED across the two places of service.
    assert p["service_count"] == 15
    assert p["beneficiary_count"] == 12
    # Facility and non-facility dollar amounts are carried SEPARATELY, not averaged.
    assert p["facility_submitted_amount"] == 200.0
    assert p["facility_allowed_amount"] == 120.0
    assert p["facility_paid_amount"] == 96.0
    assert p["nonfacility_submitted_amount"] == 150.0
    assert p["nonfacility_allowed_amount"] == 90.0
    assert p["nonfacility_paid_amount"] == 72.0


def test_single_place_of_service_leaves_other_side_none():
    rows = [_partb_row("1000000001", "99213", "O", srvcs=5, benes=4,
                       submitted=150.0, allowed=90.0, paid=72.0)]
    p = aggregate_procedures(rows)["1000000001"][0]
    assert p["nonfacility_allowed_amount"] == 90.0
    assert p["facility_allowed_amount"] is None
    assert p["facility_submitted_amount"] is None
    assert p["facility_paid_amount"] is None


def test_distinct_hcpcs_stay_separate_and_group_per_npi():
    rows = [
        _partb_row("1000000001", "99213", "F", srvcs=10, benes=8,
                   submitted=1, allowed=1, paid=1),
        _partb_row("1000000001", "99214", "F", srvcs=3, benes=2,
                   submitted=1, allowed=1, paid=1),
        _partb_row("1000000002", "99213", "O", srvcs=4, benes=4,
                   submitted=1, allowed=1, paid=1),
    ]
    out = aggregate_procedures(rows)
    assert {p["hcpcs"] for p in out["1000000001"]} == {"99213", "99214"}
    assert len(out["1000000001"]) == 2
    assert [p["hcpcs"] for p in out["1000000002"]] == ["99213"]


# --- 2. brand/generic -> one drug per (NPI, RXCUI) ---------------------------

def test_collect_drugs_is_raw_one_entry_per_row():
    rows = [
        _partd_row("1000000001", "Lipitor", "atorvastatin",
                   claims=10, days=300, benes=8, cost=500.0),
        _partd_row("1000000001", "atorvastatin calcium", "atorvastatin",
                   claims=5, days=150, benes=4, cost=120.0),
    ]
    drugs = collect_drugs(rows)["1000000001"]
    # Base build keeps rows RAW -- no collapse happens here (that is the enrich
    # pass). Two source rows -> two raw drug entries.
    assert len(drugs) == 2
    assert all(d["rxcui"] is None for d in drugs)


def test_regrain_collapses_brand_and_generic_to_one_rxcui():
    # Two Part D rows -- a brand row and a generic row of the same molecule --
    # both resolve (in the enrich pass) to the same ingredient RXCUI.
    drugs = [
        {"brand": "Lipitor", "generic": "atorvastatin", "total_claims": 10,
         "days_supply": 300, "beneficiary_count": 8, "total_drug_cost": 500.0,
         "rxcui": "83367", "molecule": "atorvastatin", "parent_indications": ["IND-HLD"]},
        {"brand": "atorvastatin calcium", "generic": "atorvastatin", "total_claims": 5,
         "days_supply": 150, "beneficiary_count": 4, "total_drug_cost": 120.0,
         "rxcui": "83367", "molecule": "atorvastatin", "parent_indications": ["IND-CVD"]},
    ]
    out = regrain_drugs_by_rxcui(drugs)
    assert len(out) == 1
    d = out[0]
    assert d["rxcui"] == "83367"
    # Measures are SUMMED across the collapsed rows (decision: sums).
    assert d["total_claims"] == 15
    assert d["days_supply"] == 450
    assert d["beneficiary_count"] == 12
    assert d["total_drug_cost"] == 620.0
    # Distinct brands collapse to ambiguous -> None; indications union.
    assert d["brand"] is None
    assert d["parent_indications"] == ["IND-CVD", "IND-HLD"]


def test_regrain_without_rxcui_collapses_by_generic_name():
    drugs = [
        {"brand": "BrandA", "generic": "metformin", "total_claims": 2,
         "days_supply": 60, "beneficiary_count": 2, "total_drug_cost": 20.0,
         "rxcui": None, "molecule": None, "parent_indications": []},
        {"brand": "BrandB", "generic": "metformin", "total_claims": 3,
         "days_supply": 90, "beneficiary_count": 3, "total_drug_cost": 30.0,
         "rxcui": None, "molecule": None, "parent_indications": []},
    ]
    out = regrain_drugs_by_rxcui(drugs)
    assert len(out) == 1
    assert out[0]["days_supply"] == 150
    assert out[0]["total_claims"] == 5


def test_regrain_keeps_distinct_rxcui_separate():
    drugs = [
        {"brand": "Lipitor", "generic": "atorvastatin", "days_supply": 300,
         "total_claims": 1, "beneficiary_count": 1, "total_drug_cost": 1.0,
         "rxcui": "83367", "molecule": "atorvastatin", "parent_indications": []},
        {"brand": "Glucophage", "generic": "metformin", "days_supply": 90,
         "total_claims": 1, "beneficiary_count": 1, "total_drug_cost": 1.0,
         "rxcui": "6809", "molecule": "metformin", "parent_indications": []},
    ]
    out = regrain_drugs_by_rxcui(drugs)
    assert {d["rxcui"] for d in out} == {"83367", "6809"}
    assert len(out) == 2


# --- 3. distinct-NPI de-dup for not-found ------------------------------------

def test_not_found_counts_each_npi_once_across_many_rows():
    # One absent NPI spanning MANY drug + procedure rows must be ONE finding.
    partb = [
        _partb_row("1000000009", "99213", "F", srvcs=1, benes=1,
                   submitted=1, allowed=1, paid=1),
        _partb_row("1000000009", "99214", "O", srvcs=1, benes=1,
                   submitted=1, allowed=1, paid=1),
    ]
    partd = [
        _partd_row("1000000009", "Lipitor", "atorvastatin",
                   claims=1, days=30, benes=1, cost=1.0),
        _partd_row("1000000009", "Glucophage", "metformin",
                   claims=1, days=30, benes=1, cost=1.0),
        # A second NPI that IS in Provider -- must not appear in not-found.
        _partd_row("1000000001", "Zocor", "simvastatin",
                   claims=1, days=30, benes=1, cost=1.0),
    ]
    billed = distinct_billed_npis(partb, partd)
    assert billed == {"1000000009", "1000000001"}

    provider_npis = {"1000000001"}  # 1000000009 absent from Provider
    not_found = npis_not_found(billed, provider_npis)
    # Exactly one entry for the absent NPI, regardless of its four source rows.
    assert not_found == ["1000000009"]


def test_not_found_empty_when_all_present():
    partb = [_partb_row("1000000001", "99213", "F", srvcs=1, benes=1,
                        submitted=1, allowed=1, paid=1)]
    partd = [_partd_row("1000000002", "Zocor", "simvastatin",
                        claims=1, days=1, benes=1, cost=1.0)]
    billed = distinct_billed_npis(partb, partd)
    assert npis_not_found(billed, {"1000000001", "1000000002"}) == []


# --- 4. (HCPCS, NUCC) / (RXCUI, NUCC) specialty rollup -----------------------

def _provider_doc(npi, specialty, procedures, drugs, indications=None):
    return {
        "_id": npi,
        "npi": npi,
        "specialty": specialty,
        "procedures": procedures,
        "drugs": drugs,
        "indications": indications or [],
    }


def test_specialty_rollup_grain_hcpcs_and_rxcui_per_nucc():
    proc = lambda hc, sc, bc, fa, nfa: {  # noqa: E731
        "hcpcs": hc, "service_count": sc, "beneficiary_count": bc,
        "facility_allowed_amount": fa, "nonfacility_allowed_amount": nfa,
    }
    drug = lambda rx, mol, claims, benes, cost, ds: {  # noqa: E731
        "rxcui": rx, "molecule": mol, "generic": mol, "total_claims": claims,
        "beneficiary_count": benes, "total_drug_cost": cost, "days_supply": ds,
    }
    docs = [
        _provider_doc("1000000001", ["207Q00000X"],
                      [proc("99213", 10, 8, 100.0, 50.0)],
                      [drug("83367", "atorvastatin", 10, 8, 500.0, 300)]),
        _provider_doc("1000000002", ["207Q00000X"],
                      [proc("99213", 5, 4, 20.0, 10.0)],
                      [drug("83367", "atorvastatin", 5, 4, 120.0, 150)]),
        _provider_doc("1000000003", ["208D00000X"],
                      [proc("99213", 1, 1, 1.0, 1.0)],
                      [drug("6809", "metformin", 2, 2, 20.0, 90)]),
    ]
    out = rollup_specialty(docs)

    assert set(out) == {"207Q00000X", "208D00000X"}

    fam = out["207Q00000X"]
    # One procedure per (HCPCS, NUCC): both family providers billed 99213.
    assert set(fam["procedures"]) == {"99213"}
    hc = fam["procedures"]["99213"]
    assert hc["provider_npis"] == {"1000000001", "1000000002"}
    assert hc["service_count"] == 15
    assert hc["beneficiary_count"] == 12
    # allowed_amount sums facility + non-facility across providers.
    assert hc["allowed_amount"] == 100.0 + 50.0 + 20.0 + 10.0

    # One drug per (RXCUI, NUCC): both providers prescribed RXCUI 83367.
    assert set(fam["drugs"]) == {"83367"}
    dr = fam["drugs"]["83367"]
    assert dr["provider_npis"] == {"1000000001", "1000000002"}
    assert dr["total_claims"] == 15
    assert dr["total_cost"] == 620.0
    # Per-provider day-supply list feeds the benchmark stats downstream.
    assert sorted(dr["days_supply_list"]) == [150, 300]

    # The pediatric NUCC rolls up its own, disjoint, drug/procedure.
    assert set(out["208D00000X"]["drugs"]) == {"6809"}
    assert out["208D00000X"]["seen_providers"] == {"1000000003"}


def test_specialty_rollup_respects_nucc_filter():
    docs = [
        _provider_doc("1000000001", ["207Q00000X", "208D00000X"], [], []),
    ]
    out = rollup_specialty(docs, nucc_codes={"207Q00000X"})
    assert set(out) == {"207Q00000X"}
