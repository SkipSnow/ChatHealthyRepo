# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""ProviderMedicare record grain (EPIC-010-F-007-S-002): one record per
molecule per provider.

Covers the pure build arithmetic against the approved requirements -- the key
(B-001), the drug-fact fold with its arrays and sums (B-006), the explicit-null
suppression rule (B-008), and conformance of a built record to the published
contract (the H3 check's schema). No Mongo: fold_rows and record_id are pure,
and the schema validation reads the committed contract file.
"""

from __future__ import annotations

import json
from pathlib import Path

import jsonschema
import pytest

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    fold_rows, record_id)

C = PARTD_COLS
CONTRACT = (Path(__file__).resolve().parents[4] / "Website" / "schemas"
            / "ChatHealthyProviderMedicareSchema.json")


def row(brand, generic, clms=None, day_suply=None, *, suppress_bene=False):
    r = {C["npi"]: "1234567890", C["brand"]: brand, C["generic"]: generic,
         C["state"]: "DE"}
    if clms is not None:
        r[C["total_claims"]] = clms
    if day_suply is not None:
        r[C["days_supply"]] = day_suply
    if suppress_bene:
        r[C["bene_sprsn_flag"]] = "*"
    return r


# ---------------------------------------------------------------- the key

def test_record_key_is_npi_and_molecule():
    assert record_id("1234567890", "lisinopril") == {
        "npi": "1234567890", "molecule_id": "lisinopril"}


def test_record_key_is_stable_and_distinguishes_both_dimensions():
    same = record_id("1234567890", "lisinopril")
    other_molecule = record_id("1234567890", "metformin")
    other_provider = record_id("1999999999", "lisinopril")
    assert same == record_id("1234567890", "lisinopril")
    assert same != other_molecule
    assert same != other_provider


# ------------------------------------------------------------- the fold

def test_brands_are_an_array_in_first_seen_order_without_duplicates():
    facts = fold_rows([row("Prinivil", "Lisinopril", 10, 300),
                       row("Zestril", "Lisinopril", 5, 150),
                       row("Prinivil", "Lisinopril", 7, 210)])
    assert facts["brand_names"] == ["Prinivil", "Zestril"]
    assert facts["generic_names"] == ["Lisinopril"]


def test_additive_quantities_sum_across_rows():
    facts = fold_rows([row("Prinivil", "Lisinopril", 10, 300),
                       row("Zestril", "Lisinopril", 5, 150),
                       row("Prinivil", "Lisinopril", 7, 210)])
    assert facts["tot_clms"] == 22
    assert facts["tot_day_suply"] == 660
    assert facts["record_count"] == 3


def test_single_row_folds_to_its_own_values():
    facts = fold_rows([row("Glucophage", "Metformin HCl", 42, 1260)])
    assert facts["tot_clms"] == 42
    assert facts["tot_day_suply"] == 1260
    assert facts["record_count"] == 1
    assert facts["brand_names"] == ["Glucophage"]


def test_blank_brand_is_not_carried_as_an_empty_string():
    facts = fold_rows([row("", "Lisinopril", 10, 300),
                       row(None, "Lisinopril", 1, 30)])
    assert facts["brand_names"] == []
    assert facts["tot_clms"] == 11


def test_every_distinct_generic_name_is_carried():
    facts = fold_rows([row("A", "Amoxicillin", 1, 10),
                       row("B", "Amoxicillin Sodium", 1, 10)])
    assert facts["generic_names"] == ["Amoxicillin", "Amoxicillin Sodium"]


# --------------------------------------------------- suppression (B-008)

def test_suppressed_quantity_in_one_row_does_not_poison_the_sum():
    facts = fold_rows([row("Prinivil", "Lisinopril", 10, 300),
                       row("Zestril", "Lisinopril", None, None)])
    assert facts["tot_clms"] == 10
    assert facts["tot_day_suply"] == 300


def test_absent_quantities_contribute_nothing_and_do_not_crash():
    """Measured 2026-10-23: Tot_Clms and Tot_Day_Suply are present on 100.0%
    of the 28,023,892 staged rows and Bene_Sprsn_Flag is set on none, so the
    carried quantities are never suppressed in practice. A blank must still
    contribute zero rather than raise."""
    facts = fold_rows([row("Prinivil", "Lisinopril", None, None),
                       row("Zestril", "Lisinopril", None, None)])
    assert facts["tot_clms"] == 0
    assert facts["tot_day_suply"] == 0
    assert facts["record_count"] == 2


def test_a_real_zero_is_kept_as_zero():
    facts = fold_rows([row("Prinivil", "Lisinopril", 0, 0)])
    assert facts["tot_clms"] == 0
    assert facts["tot_day_suply"] == 0


def test_no_beneficiary_count_is_folded():
    facts = fold_rows([row("Prinivil", "Lisinopril", 10, 300)])
    assert not [k for k in facts if "bene" in k.lower()]


# ------------------------------------------- the published contract (H3)

def _built_record(**overrides):
    rec = {
        "_id": record_id("1234567890", "lisinopril"),
        "npi": "1234567890",
        "molecule_id": "lisinopril",
        "entity_type": "I",
        "nucc_codes": ["207R00000X"],
        "medicare_population": "Part D including Medicare Advantage",
        "source_state": "DE",
        "contributed_by": ["DE"],
        **fold_rows([row("Prinivil", "Lisinopril", 10, 300)]),
    }
    rec.update(overrides)
    return rec


def _validator():
    return jsonschema.Draft202012Validator(
        json.loads(CONTRACT.read_text(encoding="utf-8")))


def test_a_built_record_conforms_to_the_published_contract():
    assert not list(_validator().iter_errors(_built_record()))


def test_record_with_suppressed_sums_conforms():
    rec = _built_record(**fold_rows([row("Prinivil", "Lisinopril", None, None)]))
    assert not list(_validator().iter_errors(rec))


def test_organizational_record_with_ccn_and_tier_conforms():
    rec = _built_record(entity_type="O", ccn="123456", size_tier=3)
    assert not list(_validator().iter_errors(rec))


def test_organizational_record_with_absent_ccn_and_tier_conforms():
    rec = _built_record(entity_type="O", ccn=None, size_tier=None)
    assert not list(_validator().iter_errors(rec))


def test_record_whose_npi_is_in_no_provider_record_conforms():
    rec = _built_record(entity_type=None, nucc_codes=[])
    assert not list(_validator().iter_errors(rec))


@pytest.mark.parametrize("missing", [
    "_id", "npi", "molecule_id", "medicare_population", "brand_names",
    "generic_names", "tot_clms", "tot_day_suply", "record_count"])
def test_contract_rejects_a_record_missing_any_required_field(missing):
    rec = _built_record()
    rec.pop(missing)
    assert list(_validator().iter_errors(rec)), (
        f"contract accepted a record with no {missing}")


def test_contract_rejects_the_retired_molecule_only_key():
    rec = _built_record(_id="lisinopril")
    assert list(_validator().iter_errors(rec))


def test_contract_rejects_the_retired_children_shape():
    rec = _built_record(children=[], state_aggregated=False,
                        applied_partitions=[])
    assert list(_validator().iter_errors(rec))


def test_contract_rejects_a_wrong_medicare_population():
    rec = _built_record(medicare_population="Part B fee-for-service")
    assert list(_validator().iter_errors(rec))


def test_contract_rejects_a_malformed_npi():
    rec = _built_record(npi="12345")
    assert list(_validator().iter_errors(rec))
