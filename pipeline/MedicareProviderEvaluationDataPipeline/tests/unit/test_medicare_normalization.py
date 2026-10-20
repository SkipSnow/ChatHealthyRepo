# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for the Medicare pure normalization core (LLD v83 Part III).

Pure functions only -- fabricated CMS rows and label results, no Mongo, no I/O
(the engines are exercised by the cluster-backed verification). Covers:
  - partd_child: verbatim quantities, suppression -> null with flag kept
  - the R1 statistics: max / min (zeros excluded) / median / avg
  - fda_molecule_union: set_ids, indication dedup + ICD-10 code union,
    drug_class SET, brand-key union, trust aggregate
  - molecule_id: the shared CMS/FDA surrogate minting
  - aggregate_contribution + raw_children_for: one partition's contribution
  - accumulate_provider_molecule_totals + rollup_specialty_v83: specialty vs
    de-duplicated real totals, prescriber counts, per-provider statistics
  - the partition marker label
"""

from __future__ import annotations

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    _stats_over,
    accumulate_provider_molecule_totals,
    brand_join_key,
    child_ge65_suppressed,
    fda_molecule_union,
    molecule_id,
    normalize_brand,
    partd_child,
    rollup_specialty_v83,
)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    aggregate_contribution,
    partition_label,
    raw_children_for,
)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_enrich_drugs_engine import (
    fda_block_from,
)

C = PARTD_COLS


def _row(npi="1003000126", state="DE", brand="LIPITOR", generic="ATORVASTATIN CALCIUM",
         clms=10, fills=12, supply=300, cost=250.5, benes=15,
         ge65_clms=6, ge65_sup="", bene_sup="", ge65_bene_sup=""):
    return {
        C["npi"]: npi, C["state"]: state, C["brand"]: brand, C["generic"]: generic,
        C["total_claims"]: clms, C["total_30day_fills"]: fills,
        C["days_supply"]: supply, C["total_drug_cost"]: cost,
        C["beneficiary_count"]: benes,
        C["ge65_total_claims"]: ge65_clms, C["ge65_total_30day_fills"]: 7,
        C["ge65_days_supply"]: 180, C["ge65_total_drug_cost"]: 120.0,
        C["ge65_beneficiary_count"]: 9,
        C["bene_sprsn_flag"]: bene_sup, C["ge65_sprsn_flag"]: ge65_sup,
        C["ge65_bene_sprsn_flag"]: ge65_bene_sup,
    }


# ---------------------------------------------------------------- partd_child
def test_partd_child_carries_quantities_verbatim():
    child = partd_child(_row())
    assert child["npi"] == "1003000126"
    assert child["tot_clms"] == 10
    assert child["tot_drug_cst"] == 250.5
    assert child["tot_benes"] == 15
    assert child["ge65_tot_clms"] == 6
    assert not child_ge65_suppressed(child)


def test_partd_child_suppression_is_null_with_flag_kept_never_zero():
    child = partd_child(_row(ge65_sup="*", bene_sup="*", ge65_bene_sup="*"))
    assert child["tot_benes"] is None
    assert child["ge65_tot_clms"] is None
    assert child["ge65_tot_benes"] is None
    assert child["suppression_flags"]["GE65_Sprsn_Flag"] == "*"
    assert child_ge65_suppressed(child)
    # all-ages usage is never suppressed and stays verbatim
    assert child["tot_clms"] == 10


# ------------------------------------------------------------- R1 statistics
def test_stats_max_min_median_avg_definitions():
    s = _stats_over([4, 1, 0, 9, 2])
    assert s["max"] == 9
    # a 0 is not a min -- zeros are excluded
    assert s["min"] == 1
    # median = the middle value (over ALL values incl. zero)
    assert s["median"] == 2
    # avg = sum / count
    assert s["avg"] == (4 + 1 + 0 + 9 + 2) / 5


def test_stats_all_zero_min_is_null():
    s = _stats_over([0, 0])
    assert s["min"] is None
    assert s["max"] == 0


def test_stats_empty_is_all_null():
    s = _stats_over([])
    assert s == {"max": None, "min": None, "median": None, "avg": None}


# --------------------------------------------------------------- molecule_id
def test_molecule_id_folds_cms_and_fda_spellings():
    assert molecule_id("ATORVASTATIN CALCIUM") == molecule_id("atorvastatin")
    assert molecule_id("") is None
    assert normalize_brand("x  AND  y") == "X-Y"
    assert brand_join_key("LIPITOR ER") == "LIPITOR"


# -------------------------------------------------------- fda_molecule_union
def _label(set_id, molecule="atorvastatin", source="substance_name",
           brand="LIPITOR", conf=0.9, ind_text="treatment of hyperlipidemia",
           codes=None, cls="RX"):
    return {
        "set_id": set_id,
        "molecule": molecule,
        "molecule_source": source,
        "openfda": {"brand_name": [brand], "generic_name": ["atorvastatin"],
                    "rxcui": ["83367"], "unii": ["A0JWA85V8F"],
                    "substance_name": ["ATORVASTATIN CALCIUM"]},
        "indications": [{
            "indication": ind_text,
            "confidence_rationale": "label states it",
            "codes": codes if codes is not None else [
                {"icd10_code": "E78", "level": 3, "confidence": 0.95,
                 "description": "Disorders of lipoprotein metabolism"}],
        }],
        "usage_summary": "lowers LDL",
        "resolution_confidence": conf,
        "drug_class": cls,
    }


def test_union_set_ids_are_the_contributing_keys():
    doc = fda_molecule_union([_label("s1"), _label("s2"), _label("s1")])
    assert doc["set_ids"] == ["s1", "s2"]   # folded once each


def test_union_indications_dedup_by_text_and_union_codes():
    a = _label("s1", codes=[{"icd10_code": "E78", "level": 3, "confidence": 0.95,
                             "description": None}])
    b = _label("s2", ind_text="Treatment of  HYPERLIPIDEMIA",
               codes=[{"icd10_code": "E78.5", "level": 4, "confidence": 0.8,
                       "description": None}])
    doc = fda_molecule_union([a, b])
    assert len(doc["indications"]) == 1     # deduped by normalized text
    ind = doc["indications"][0]
    assert ind["icd10"]["level3"] == ["E78"]
    assert ind["icd10"]["level4"] == ["E78.5"]
    assert ind["_crosswalk_provenance"] == "inference"
    assert ind["trust"] == 0.8              # the weakest code confidence


def test_union_drug_class_is_a_set_and_trust_aggregates():
    doc = fda_molecule_union([
        _label("s1", cls="RX", conf=0.9),
        _label("s2", cls="OTC", conf=0.5),
        _label("s3", cls="RX", conf=0.7),
    ])
    assert doc["drug_class"]["classes"] == ["RX", "OTC"]
    assert doc["trust"]["max"] == 0.9
    assert doc["trust"]["min"] == 0.5
    assert doc["trust"]["median"] == 0.7
    assert abs(doc["trust"]["avg"] - (0.9 + 0.5 + 0.7) / 3) < 1e-9
    assert doc["brand_exact_keys"] == ["LIPITOR"]
    assert "lowers LDL" in doc["usage"]["summarized_usage"]


# ------------------------------------------- partition contribution + children
def test_partition_label_shape():
    assert partition_label("DE", 1) == "DE:1"
    assert partition_label("ALL", 2) == "ALL:2"


def test_aggregate_contribution_sums_allages_and_visible_ge65_only():
    rows = [_row(clms=10, ge65_clms=6),
            _row(clms=5, ge65_sup="*")]       # GE65 suppressed: excluded, counted
    c = aggregate_contribution(rows)
    assert c["tot_clms"] == 15
    assert c["ge65_tot_clms"] == 6            # visible rows only
    assert c["ge65_usage_suppressed_count"] == 1
    assert c["record_count"] == 2


def test_raw_children_carry_business_state_and_cms_state_verbatim():
    rows = [_row(state="FL")]                  # CMS says FL ...
    children = raw_children_for(rows, "DE", {"1003000126": ["207Q00000X"]})
    child = children[0]
    assert child["state"] == "DE"              # ... partition basis is business state
    assert child["cms_state"] == "FL"          # CMS column kept verbatim, source only
    assert child["nucc_codes"] == ["207Q00000X"]


# ------------------------------------------------------------- rollup (v83)
def test_rollup_specialty_vs_real_dedup_and_stats():
    rows = [
        _row(npi="A", generic="ATORVASTATIN", clms=10, fills=1, supply=1, cost=1),
        _row(npi="A", generic="ATORVASTATIN", clms=20, fills=1, supply=1, cost=1),
        _row(npi="B", generic="ATORVASTATIN", clms=40, fills=1, supply=1, cost=1),
    ]
    acc = accumulate_provider_molecule_totals(rows)
    mid = molecule_id("ATORVASTATIN")
    assert acc[("A", mid)]["tot_clms"] == 30
    assert acc[("B", mid)]["tot_clms"] == 40

    # A carries two NUCCs (attributed to EACH); B carries one of them.
    out = rollup_specialty_v83(acc, {"A": ["N1", "N2"], "B": ["N1"]})
    n1 = out["N1"][mid]
    n2 = out["N2"][mid]
    assert n1["specialty_total"]["tot_clms"] == 70      # A + B
    assert n2["specialty_total"]["tot_clms"] == 30      # A only (overlap)
    # the real side de-duplicates: each provider once
    assert n1["molecule_real_total"]["tot_clms"] == 70
    assert n2["molecule_real_total"]["tot_clms"] == 70
    assert n1["prescriber_count"] == 2
    assert n2["prescriber_count"] == 1
    # statistics over PER-PROVIDER totals [30, 40]
    assert n1["specialty_stats"]["tot_clms"]["max"] == 40
    assert n1["specialty_stats"]["tot_clms"]["min"] == 30
    assert n1["specialty_stats"]["tot_clms"]["median"] == 35
    assert n1["specialty_stats"]["tot_clms"]["avg"] == 35
    assert n1["molecule_real_stats"]["tot_clms"]["median"] == 35


def test_rollup_skips_rows_without_molecule_or_npi():
    rows = [_row(npi="", clms=1), _row(generic="", clms=2), _row(clms=3)]
    acc = accumulate_provider_molecule_totals(rows)
    assert len(acc) == 1   # only the complete row lands


# --------------------------------------------------------------- fda_block
def test_fda_block_from_molecule_record():
    fda_doc = fda_molecule_union([_label("s1", conf=0.9)])
    block = fda_block_from(fda_doc)
    assert block["drug_class"] == ["RX"]
    assert block["identity"]["rxcui"] == ["83367"]
    assert block["identity"]["unii"] == ["A0JWA85V8F"]
    assert block["trust"]["max"] == 0.9
    assert block["usage"] == "lowers LDL"
    assert block["indications"][0]["icd10"]["level3"] == ["E78"]
