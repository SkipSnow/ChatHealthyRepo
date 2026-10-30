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

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_store_contract import (
    json_view)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    _stats_over,
    accumulate_provider_molecule_totals,
    fda_molecule_union,
    molecule_id,
    rollup_specialty_v83,
)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    fold_rows,
    record_id,
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
    # The contract describes the JSON view, in which an exact decimal is a
    # string; the record carries Decimal. json_view renders one from the other.
    doc = json_view(doc)
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


def _label(set_id, confidence=0.95):
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
                                    "confidence": confidence, "description": None}]}],
        "usage_summary": "lowers LDL",
        "drug_class": "RX",
    }


def _fda_doc(confidence=0.95):
    """The exact document finalize_fda_molecules writes."""
    mid = molecule_id("atorvastatin")
    doc = fda_molecule_union([_label("s1", confidence),
                              _label("s2", confidence)])
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


def test_an_integral_crosswalk_confidence_still_conforms():
    """A confidence of exactly 1.0 or 0.0 is an ordinary model answer.

    The crosswalk is asked for a confidence from 0 to 1 on each ICD-10
    code, and full certainty on an obvious mapping -- \"for the treatment of
    hypertension\" -> I10 -- is a normal outcome, as is a flat refusal to
    guess. Every fixture in this suite pinned a FRACTIONAL confidence, so
    the type boundary was never crossed and this file -- whose stated
    purpose is that a build path producing a document the contract rejects
    fails HERE, before any run exists -- passed while the build shipped a
    Python int. The store refused one molecule of 20,142 on 2026-10-29 and
    ended a run 3h24m old with its collection already dropped.
    """
    for confidence in (1.0, 0.0, 1, 0):
        _assert_valid(_fda_doc(confidence),
                      "ChatHealthyFdaProductToDiagnosisResolutionSchema.json")



def _providermedicare_record(npi: str, mid: str, rows: list[dict]) -> dict:
    """The exact record build_providermedicare_base writes (REQ-B-001)."""
    return {
        "_id": record_id(npi, mid),
        "npi": npi,
        "molecule_id": mid,
        "entity_type": "I",
        "nucc_codes": ["207Q00000X"],
        "medicare_population": "Part D including Medicare Advantage",
        "source_state": "DE",
        "contributed_by": ["DE"],
        **fold_rows(rows),
    }


def test_providermedicare_record_conforms():
    mid = molecule_id("atorvastatin")
    doc = _providermedicare_record("1003000126", mid, [_row()])
    _assert_valid(doc, "ChatHealthyProviderMedicareSchema.json")


def test_providermedicare_record_after_enrich_conforms():
    mid = molecule_id("atorvastatin")
    doc = _providermedicare_record("1003000126", mid, [_row(), _row()])
    doc["fda"] = fda_block_from(_fda_doc())
    _assert_valid(doc, "ChatHealthyProviderMedicareSchema.json")


def test_two_providers_of_one_molecule_are_two_conforming_records():
    mid = molecule_id("atorvastatin")
    a = _providermedicare_record("1003000126", mid, [_row()])
    b = _providermedicare_record("1104000127", mid,
                                 [_row(npi="1104000127")])
    assert a["_id"] != b["_id"]
    for doc in (a, b):
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
        # keyed by molecule: the rollup fans out by molecule and each
        # worker writes only the keys it owns
        "molecules": {mid: by_nucc[nucc][mid] for mid in by_nucc[nucc]},
        "provider_count": 2,
        "provenance": {"aggregated_from": ["StagingMedicarePartD_v_1", "Provider_v_5"],
                       "run_id": "run-x",
                       "built_at": datetime.datetime.utcnow().isoformat()},
    }
    _assert_valid(doc, "ChatHealthySpecialtyMedicareSchema.json")


def test_stats_shape_matches_schema_fields():
    s = _stats_over([1, 2, 3])
    assert set(s) == {"max", "min", "median", "avg"}
