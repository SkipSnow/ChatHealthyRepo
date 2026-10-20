# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Code-vs-contract conformance at the unit tier (LLD v83 III.4/III.6 H3).

Builds representative documents with the SAME pure functions and document
shapes the engines write, and validates them against the committed JSON
Schemas in Website/schemas. This is the drift detector: a build path that
produces a document the contract rejects fails HERE, before any run exists
(a run-time schema violation is hard-fail H3 -- a software bug)."""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    _stats_over,
    accumulate_provider_molecule_totals,
    fda_molecule_union,
    molecule_id,
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

_SCHEMA_DIR = Path(__file__).resolve().parents[4] / "Website" / "schemas"
C = PARTD_COLS


def _validator(name: str):
    from jsonschema import Draft202012Validator
    schema = json.loads((_SCHEMA_DIR / name).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def _assert_valid(doc: dict, schema_file: str) -> None:
    errors = sorted(_validator(schema_file).iter_errors(doc), key=lambda e: e.path)
    assert not errors, f"{schema_file}: {list(errors[0].path)}: {errors[0].message}"


def _row(npi="1003000126", generic="ATORVASTATIN CALCIUM"):
    return {
        C["npi"]: npi, C["state"]: "FL", C["brand"]: "LIPITOR", C["generic"]: generic,
        C["total_claims"]: 11, C["total_30day_fills"]: 12,
        C["days_supply"]: 300, C["total_drug_cost"]: 250.5,
        C["beneficiary_count"]: 15,
        C["ge65_total_claims"]: 6, C["ge65_total_30day_fills"]: 7,
        C["ge65_days_supply"]: 180, C["ge65_total_drug_cost"]: 120.0,
        C["ge65_beneficiary_count"]: 9,
        C["bene_sprsn_flag"]: "", C["ge65_sprsn_flag"]: "", C["ge65_bene_sprsn_flag"]: "",
    }


def _label(set_id):
    return {
        "set_id": set_id,
        "molecule": "atorvastatin",
        "molecule_source": "substance_name",
        "openfda": {"brand_name": ["LIPITOR"], "generic_name": ["atorvastatin"],
                    "rxcui": ["83367"], "unii": ["A0JWA85V8F"],
                    "substance_name": ["ATORVASTATIN CALCIUM"]},
        "indications": [{"indication": "treatment of hyperlipidemia",
                         "confidence_rationale": "label",
                         "codes": [{"icd10_code": "E78", "level": 3,
                                    "confidence": 0.95, "description": None}]}],
        "usage_summary": "lowers LDL",
        "resolution_confidence": 0.9,
        "drug_class": "RX",
    }


def _fda_doc():
    """The exact document finalize_fda_molecules writes."""
    mid = molecule_id("atorvastatin")
    doc = fda_molecule_union([_label("s1"), _label("s2")])
    doc["_id"] = mid
    doc["molecule_id"] = mid
    doc["run_id"] = "run-x"
    doc["data_version"] = 1
    doc["provenance"] = {"source_run_id": "run-x",
                         "source_collection": "StagingOpenFdaLabels_v_1",
                         "built_at": datetime.datetime.utcnow().isoformat()}
    return doc


def test_fda_molecule_document_conforms():
    _assert_valid(_fda_doc(), "ChatHealthyFdaProductToDiagnosisResolutionSchema.json")


def _master_stub(mid: str, state_aggregated: bool):
    """The exact stub prepare_molecule_masters writes."""
    return {
        "_id": mid,
        "molecule_id": mid,
        "fda": {"indications": [], "drug_class": [],
                "trust": {"max": None, "min": None, "median": None, "avg": None}},
        "totals": {"tot_clms": 0, "tot_30day_fills": 0,
                   "tot_day_suply": 0, "tot_drug_cst": 0},
        "total_cms_record_count": 0,
        "ge65_usage_suppressed_count": 0,
        "state_aggregated": state_aggregated,
        "applied_partitions": [],
        "children": [],
        "provenance": {"source_run_id": "run-x",
                       "source_collection": "StagingMedicarePartD_v_1",
                       "built_at": datetime.datetime.utcnow().isoformat()},
    }


def test_master_stub_conforms():
    _assert_valid(_master_stub(molecule_id("atorvastatin"), False),
                  "ChatHealthyProviderMedicareSchema.json")


def test_master_after_raw_apply_and_enrich_conforms():
    mid = molecule_id("atorvastatin")
    doc = _master_stub(mid, state_aggregated=False)
    rows = [_row(), _row(npi="1104000127")]
    contrib = aggregate_contribution(rows)
    # the raw-mode apply, replayed client-side exactly as the engine's update
    doc["children"] = raw_children_for(rows, "DE", {"1003000126": ["207Q00000X"],
                                                    "1104000127": []})
    doc["applied_partitions"].append(partition_label("DE", 1))
    for k in ("tot_clms", "tot_30day_fills", "tot_day_suply", "tot_drug_cst"):
        doc["totals"][k] += contrib[k]
    doc["total_cms_record_count"] += contrib["record_count"]
    doc["ge65_usage_suppressed_count"] += contrib["ge65_usage_suppressed_count"]
    # the enrich attach
    doc["fda"] = fda_block_from(_fda_doc())
    _assert_valid(doc, "ChatHealthyProviderMedicareSchema.json")


def test_master_after_aggregate_apply_conforms():
    mid = molecule_id("atorvastatin")
    doc = _master_stub(mid, state_aggregated=True)
    contrib = aggregate_contribution([_row(), _row(npi="1104000127")])
    child = {"state": "DE", "record_count": contrib["record_count"],
             "tot_clms": contrib["tot_clms"],
             "tot_30day_fills": contrib["tot_30day_fills"],
             "tot_day_suply": contrib["tot_day_suply"],
             "tot_drug_cst": contrib["tot_drug_cst"],
             "ge65_tot_clms": contrib["ge65_tot_clms"],
             "ge65_tot_30day_fills": contrib["ge65_tot_30day_fills"],
             "ge65_tot_day_suply": contrib["ge65_tot_day_suply"],
             "ge65_tot_drug_cst": contrib["ge65_tot_drug_cst"],
             "ge65_usage_suppressed_count": contrib["ge65_usage_suppressed_count"]}
    doc["children"] = [child]
    doc["applied_partitions"] = [partition_label("DE", 1), partition_label("DE", 2)]
    doc["total_cms_record_count"] = contrib["record_count"]
    _assert_valid(doc, "ChatHealthyProviderMedicareSchema.json")


def test_specialty_document_conforms():
    rows = [_row(), _row(npi="1104000127")]
    acc = accumulate_provider_molecule_totals(rows)
    by_nucc = rollup_specialty_v83(acc, {"1003000126": ["207Q00000X"],
                                         "1104000127": ["207Q00000X"]})
    nucc = "207Q00000X"
    doc = {
        "_id": nucc,
        "nucc_code": nucc,
        "molecules": [by_nucc[nucc][mid] for mid in sorted(by_nucc[nucc])],
        "provider_count": 2,
        "provenance": {"aggregated_from": ["StagingMedicarePartD_v_1", "Provider_v_5"],
                       "run_id": "run-x",
                       "built_at": datetime.datetime.utcnow().isoformat()},
    }
    _assert_valid(doc, "ChatHealthySpecialtyMedicareSchema.json")


def test_stats_shape_matches_schema_fields():
    s = _stats_over([1, 2, 3])
    assert set(s) == {"max", "min", "median", "avg"}
