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
  2. brand normalization + FDA brand inner-join -> one drug per (NPI, brand)
  3. distinct-NPI de-dup         -> one not-found finding per NPI across N rows
  4. the (HCPCS, NUCC) / (RXCUI, NUCC) specialty rollup grain
"""

from __future__ import annotations

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTB_COLS,
    PARTD_COLS,
    aggregate_fda_records,
    aggregate_procedures,
    brand_join_key,
    collect_drugs,
    compose_drugs_by_brand,
    distinct_billed_npis,
    normalize_brand,
    normalized_brand_exact_keys,
    normalized_brand_keys,
    npis_not_found,
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


# --- 2. brand normalization + FDA brand join -> one drug per (NPI, brand) -----

def test_collect_drugs_is_raw_one_entry_per_row():
    rows = [
        _partd_row("1000000001", "Lipitor", "atorvastatin",
                   claims=10, days=300, benes=8, cost=500.0),
        _partd_row("1000000001", "atorvastatin calcium", "atorvastatin",
                   claims=5, days=150, benes=4, cost=120.0),
    ]
    drugs = collect_drugs(rows)["1000000001"]
    # Base build keeps rows RAW -- no collapse happens here (that is the enrich
    # pass). Two source rows -> two raw drug entries, molecule/rxcuis/indications
    # left empty for the enrich pass to fill.
    assert len(drugs) == 2
    assert all(d["molecule"] is None for d in drugs)
    assert all(d["rxcuis"] == [] for d in drugs)
    assert all(d["indications"] == [] for d in drugs)


def test_brand_normalization_and_join_key():
    # Combination spelling reconciliation: "X AND Y" and "X - Y" -> "X-Y".
    assert normalize_brand("amlodipine and benazepril") == "AMLODIPINE-BENAZEPRIL"
    assert normalize_brand("amlodipine - benazepril") == "AMLODIPINE-BENAZEPRIL"
    # Join key strips salt and dose-form suffix tokens.
    assert brand_join_key("Atorvastatin Calcium") == "ATORVASTATIN"
    assert brand_join_key("Lipitor ER") == "LIPITOR"
    assert brand_join_key("Lipitor") == "LIPITOR"
    # At least one token is always kept.
    assert brand_join_key("HCL") == "HCL"


def test_persisted_fda_join_keys():
    # Tier 1 (brand_exact_key): normalize_brand only -- NO salt/form strip.
    assert normalized_brand_exact_keys(["Lipitor", "LIPITOR ER"]) == ["LIPITOR", "LIPITOR ER"]
    # Tier 2/3 (brand_key / generic_key): brand_join_key -- salt/form stripped, so
    # both collapse to one key.
    assert normalized_brand_keys(["Lipitor", "LIPITOR ER"]) == ["LIPITOR"]
    assert normalized_brand_keys(["atorvastatin calcium"]) == ["ATORVASTATIN"]
    # Both sides normalize identically, so a CMS brand's key equals the FDA key the
    # build persisted -- the exact-equality join: CMS "metformin hcl" -> "METFORMIN"
    # matches the generic_key the build wrote for FDA generic_name "metformin".
    assert brand_join_key("metformin hcl") == normalized_brand_keys(["metformin"])[0]


def test_aggregate_fda_records_full_payload_union_and_dedup():
    fda_docs = [
        {"openfda": {"brand_name": ["Lipitor"], "generic_name": ["atorvastatin"],
                     "substance_name": ["ATORVASTATIN CALCIUM"], "product_type": ["HUMAN PRESCRIPTION DRUG"],
                     "rxcui": ["617312"], "product_ndc": ["0071-0155"],
                     "application_number": ["NDA020702"], "spl_id": ["spl-1"],
                     "spl_set_id": ["set-1"]},
         "molecule": "atorvastatin", "ingredient_rxcuis": ["83367"],
         "ingredient_names": ["atorvastatin"], "usage_summary": "Lowers lipids.",
         "indications": [
             {"indication": "Hyperlipidemia", "confidence_rationale": "clear",
              "codes": [{"icd10_code": "E78", "level": 3, "confidence": 0.9, "description": "Lipid disorders"}]},
             {"indication": "Cardiovascular risk", "confidence_rationale": "clear", "codes": []}]},
        # A repackager duplicate under the same brand: shared rxcui/ingredient, a
        # new clinical rxcui and NDC, a phrasing variant of one indication (whose
        # codes must MERGE) plus a new indication.
        {"openfda": {"brand_name": ["LIPITOR"], "generic_name": ["atorvastatin"],
                     "substance_name": ["ATORVASTATIN CALCIUM"], "product_type": ["HUMAN PRESCRIPTION DRUG"],
                     "rxcui": ["617318"], "product_ndc": ["63304-828"],
                     "application_number": ["NDA020702"], "spl_id": ["spl-2"],
                     "spl_set_id": ["set-1"]},
         "molecule": "atorvastatin", "ingredient_rxcuis": ["83367"],
         "ingredient_names": ["atorvastatin"], "usage_summary": "ignored second summary",
         "indications": [
             {"indication": "hyperlipidemia", "confidence_rationale": "repeat",
              "codes": [{"icd10_code": "E780", "level": 4, "confidence": 0.8, "description": "Pure hyperchol"}]},
             {"indication": "Primary prevention", "confidence_rationale": "ok", "codes": []}]},
    ]
    # aggregate_fda_records takes ONE brand's matched FDA label records (as an
    # indexed query returns them) and folds them into a single brand entry.
    entry = aggregate_fda_records(fda_docs)
    assert entry is not None
    # Canonical brand is the FDA brand_name (deterministic first-by-sort).
    assert entry["brand"] == "LIPITOR"
    assert entry["molecule"] == "atorvastatin"
    assert entry["rxcuis"] == ["83367"]
    assert entry["ingredient_names"] == ["atorvastatin"]
    assert entry["usage_summary"] == "Lowers lipids."  # first non-empty kept
    # openfda sub-fields carried FLAT and unioned first-seen.
    assert entry["clinical_rxcui"] == ["617312", "617318"]
    assert entry["generic_name"] == ["atorvastatin"]
    assert entry["substance_name"] == ["ATORVASTATIN CALCIUM"]
    assert entry["product_type"] == ["HUMAN PRESCRIPTION DRUG"]
    assert entry["product_ndc"] == ["0071-0155", "63304-828"]
    assert entry["application_number"] == ["NDA020702"]
    assert entry["spl_id"] == ["spl-1", "spl-2"]
    assert entry["spl_set_id"] == ["set-1"]
    # Indications: FULL objects, union across records, deduped by normalized text.
    assert [i["indication"] for i in entry["indications"]] == [
        "Hyperlipidemia", "Cardiovascular risk", "Primary prevention"]
    # The two spellings of Hyperlipidemia merge; their codes UNION by icd10_code.
    hyperlip = entry["indications"][0]
    assert [c["icd10_code"] for c in hyperlip["codes"]] == ["E78", "E780"]
    assert hyperlip["codes"][0] == {
        "icd10_code": "E78", "level": 3, "confidence": 0.9, "description": "Lipid disorders"}
    # No records -> no entry.
    assert aggregate_fda_records([]) is None


def test_compose_drugs_by_brand_sums_and_inner_joins():
    raw = [
        {"brand": "Lipitor", "generic": "atorvastatin", "total_claims": 10,
         "days_supply": 300, "beneficiary_count": 8, "total_drug_cost": 500.0},
        # A dose-form variant of the same brand normalizes to the same join key.
        {"brand": "LIPITOR ER", "generic": "atorvastatin", "total_claims": 5,
         "days_supply": 150, "beneficiary_count": 4, "total_drug_cost": 120.0},
        # A brand with no FDA match -> discarded.
        {"brand": "Mystery Pill", "generic": "unobtanium", "total_claims": 3,
         "days_supply": 90, "beneficiary_count": 3, "total_drug_cost": 30.0},
    ]
    lipitor_entry = {
        "brand": "LIPITOR", "match_provenance": "brand_name",
        "molecule": "atorvastatin", "rxcuis": ["83367"],
        "ingredient_names": ["atorvastatin"], "usage_summary": "Lowers lipids.",
        "clinical_rxcui": ["617312"], "generic_name": ["atorvastatin"],
        "substance_name": ["ATORVASTATIN CALCIUM"], "product_type": ["HUMAN PRESCRIPTION DRUG"],
        "product_ndc": ["0071-0155"], "application_number": ["NDA020702"],
        "spl_id": ["spl-1"], "spl_set_id": ["set-1"],
        "indications": [{"indication": "Hyperlipidemia", "confidence_rationale": "clear",
                         "codes": [{"icd10_code": "E78", "level": 3,
                                    "confidence": 0.9, "description": "Lipid disorders"}]}],
    }
    # The resolver maps a normalized brand join key to the FDA payload (or None),
    # exactly as the engine's cached indexed-query resolver does.
    lookup = {"LIPITOR": lipitor_entry}

    def resolve(cms_brand):
        return lookup.get(brand_join_key(cms_brand))

    kept, discarded = compose_drugs_by_brand(raw, resolve)
    assert len(kept) == 1
    d = kept[0]
    # The two matching rows SUM their volume metrics.
    assert d["total_claims"] == 15
    assert d["days_supply"] == 450
    assert d["beneficiary_count"] == 12
    assert d["total_drug_cost"] == 620.0
    # Full FDA payload carried FLAT onto the composed drug entry.
    assert d["molecule"] == "atorvastatin"
    assert d["rxcuis"] == ["83367"]
    assert d["ingredient_names"] == ["atorvastatin"]
    assert d["usage_summary"] == "Lowers lipids."
    assert d["clinical_rxcui"] == ["617312"]
    assert d["generic_name"] == ["atorvastatin"]
    assert d["substance_name"] == ["ATORVASTATIN CALCIUM"]
    assert d["product_type"] == ["HUMAN PRESCRIPTION DRUG"]
    assert d["product_ndc"] == ["0071-0155"]
    assert d["application_number"] == ["NDA020702"]
    assert d["spl_id"] == ["spl-1"]
    assert d["spl_set_id"] == ["set-1"]
    # Indications kept as FULL objects with their ICD-10 codes.
    assert d["indications"][0]["indication"] == "Hyperlipidemia"
    assert d["indications"][0]["codes"][0]["icd10_code"] == "E78"
    # The matched tier is recorded on the entry.
    assert d["match_provenance"] == "brand_name"
    # The unmatched brand is discarded and reported, not written.
    assert len(discarded) == 1
    assert discarded[0]["brand"] == "MYSTERY PILL"
    assert discarded[0]["brand_join_key"] == "MYSTERY PILL"
    assert discarded[0]["total_claims"] == 3


def test_compose_generic_match_adopts_fda_brand():
    # CMS put the GENERIC name in Brnd_Name; the resolver (generic leg) still
    # resolves it, and the composed record adopts the FDA brand name as canonical.
    raw = [
        {"brand": "metformin hcl", "generic": "metformin", "total_claims": 7,
         "days_supply": 210, "beneficiary_count": 6, "total_drug_cost": 70.0},
        # A branded fill of the SAME drug: must MERGE under the canonical FDA brand,
        # not create a duplicate canonical-brand entry.
        {"brand": "Glucophage", "generic": "metformin", "total_claims": 3,
         "days_supply": 90, "beneficiary_count": 2, "total_drug_cost": 30.0},
    ]
    glucophage = {
        "brand": "GLUCOPHAGE", "match_provenance": "generic_name",
        "molecule": "metformin", "rxcuis": ["6809"],
        "ingredient_names": ["metformin"], "usage_summary": None,
        "clinical_rxcui": [], "generic_name": ["metformin"], "substance_name": [],
        "product_type": [], "product_ndc": [], "application_number": [],
        "spl_id": [], "spl_set_id": [],
        "indications": [{"indication": "Type 2 diabetes", "confidence_rationale": None,
                         "codes": [{"icd10_code": "E11", "level": 3,
                                    "confidence": 0.95, "description": "T2DM"}]}],
    }
    # Both the generic key (METFORMIN) and the brand key (GLUCOPHAGE) resolve to
    # the same FDA payload, as the engine's brand-then-generic resolver would.
    lookup = {"METFORMIN": glucophage, "GLUCOPHAGE": glucophage}

    def resolve(cms_brand):
        return lookup.get(brand_join_key(cms_brand))

    kept, discarded = compose_drugs_by_brand(raw, resolve)
    assert discarded == []
    # Generic fill + branded fill MERGE into ONE entry under the canonical brand.
    assert len(kept) == 1
    d = kept[0]
    assert d["brand"] == "GLUCOPHAGE"
    assert d["match_provenance"] == "generic_name"
    assert d["total_claims"] == 10
    assert d["days_supply"] == 300
    assert d["molecule"] == "metformin"
    assert d["rxcuis"] == ["6809"]
    assert d["indications"][0]["codes"][0]["icd10_code"] == "E11"


def test_compose_carries_each_match_provenance():
    # Each tier's provenance (brand_name, brand_key, generic_name), as the resolver
    # stamps it, propagates onto the composed drug entry.
    def _entry(brand, prov):
        base = {"brand": brand, "match_provenance": prov, "molecule": "m",
                "rxcuis": [], "ingredient_names": [], "usage_summary": None,
                "indications": []}
        for flat in ("clinical_rxcui", "generic_name", "substance_name", "product_type",
                     "product_ndc", "application_number", "spl_id", "spl_set_id"):
            base[flat] = []
        return base

    payloads = {
        "AAA": _entry("AAA", "brand_name"),
        "BBB": _entry("BBB", "brand_key"),
        "CCC": _entry("CCC", "generic_name"),
    }
    raw = [
        {"brand": "aaa", "generic": "g1", "total_claims": 1, "days_supply": 1,
         "beneficiary_count": 1, "total_drug_cost": 1.0},
        {"brand": "bbb", "generic": "g2", "total_claims": 1, "days_supply": 1,
         "beneficiary_count": 1, "total_drug_cost": 1.0},
        {"brand": "ccc", "generic": "g3", "total_claims": 1, "days_supply": 1,
         "beneficiary_count": 1, "total_drug_cost": 1.0},
    ]
    kept, discarded = compose_drugs_by_brand(raw, lambda b: payloads.get(brand_join_key(b)))
    assert discarded == []
    by_brand = {d["brand"]: d["match_provenance"] for d in kept}
    assert by_brand == {"AAA": "brand_name", "BBB": "brand_key", "CCC": "generic_name"}


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
