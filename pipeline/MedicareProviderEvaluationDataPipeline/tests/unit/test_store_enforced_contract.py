# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The store enforces the contract (EPIC-010-F-001-S-012-REQ-B-002,
LLD v83 I.3.12).

Three things are provable without a database and all three are proved here:

  * THE TRANSLATION. A published contract restated as the $jsonSchema MongoDB
    will accept -- integers named as the BSON types they land as, nested
    objects and array items recursed into, and an untranslatable construct
    REFUSED rather than dropped. A validator that silently permits what the
    contract forbids reads as proof and is not, so the refusal is the point.
  * THE TRANSLATED VALIDATOR STILL BITES. A record built by the same pure
    functions the engines write with satisfies it, and a malformed one does
    not -- missing a required field, carrying an undeclared one, holding the
    wrong type, breaking an enum, breaking a pattern. jsonschema is the
    arbiter, reading the TRANSLATED validator rather than the contract, so
    what is checked is what MongoDB would be given.
  * THE FINDING. A refused write becomes a per-record finding carrying the
    record's business key, one finding per refused DOCUMENT, under the
    finding class the pipeline's configuration already declares.

NO MONGO DOUBLE ANYWHERE. Rule-065 statement 3 forbids standing a fake, mock,
stub or in-memory double in place of a client, database or collection, and it
exempts tests from nothing. Every part exercised here is pure: the translation
reads a committed file, and a BulkWriteError's `details` is a plain dict, so a
rejection payload is built rather than provoked. record_discrepancy is a
callback the engines are handed, so collecting its calls in a list stands in
for no collection.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import jsonschema
import pytest
from chathealthy_lib.exceptions import ChatHealthyException

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    accumulate_provider_molecule_totals,
    molecule_id,
    rollup_specialty_v83,
)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (
    record_document,
    record_key,
    fold_rows,
)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_store_contract import (
    DOCUMENT_FAILED_VALIDATION,
    PUBLISHED_CONTRACTS,
    json_view,
    STORE_REFUSAL_FINDING,
    load_contract,
    record_refused_writes,
    refusals_and_other_errors,
    to_mongo_validator,
    validator_for,
)

C = PARTD_COLS
_CONFIG = (Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts"
           / "content" / "pipeline_config.json")

# The inverse of the one type mapping the translation emits, so jsonschema can
# read the translated validator and be the arbiter of the whole of it rather
# than of the half it happens to recognise. bsonType is MongoDB's keyword and
# jsonschema would ignore it, which would quietly stop checking types.
# decimal maps to string because that is the JSON view of an exact decimal:
# json_view renders a Decimal as its own digits in a string, which is what
# this contract declares and what a JSON consumer receives.
_JSON_TYPE_OF = {"int": "integer", "long": "integer", "number": "number",
                 "bool": "boolean", "object": "object", "array": "array",
                 "string": "string", "null": "null", "decimal": "string"}


def _as_json_schema(node):
    """The translated validator, readable by jsonschema (test-local only)."""
    if isinstance(node, list):
        return [_as_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    out = {}
    for keyword, value in node.items():
        if keyword == "bsonType":
            names = value if isinstance(value, list) else [value]
            mapped = []
            for name in names:
                json_name = _JSON_TYPE_OF[name]
                if json_name not in mapped:
                    mapped.append(json_name)
            out["type"] = mapped[0] if len(mapped) == 1 else mapped
        elif keyword in ("properties",):
            out[keyword] = {k: _as_json_schema(v) for k, v in value.items()}
        elif keyword in ("items", "additionalProperties"):
            out[keyword] = _as_json_schema(value)
        else:
            out[keyword] = value
    return out


def _checker(source_name: str):
    schema = _as_json_schema(validator_for(source_name)["$jsonSchema"])
    return jsonschema.Draft202012Validator(schema)


def _errors(source_name: str, doc: dict) -> list[str]:
    # The record carries Decimal; the contract describes the JSON view, where
    # an exact decimal is a string. json_view is the one converter between
    # them, so the check reads the same view the contract declares.
    return [f"{list(e.path)}: {e.message}"
            for e in _checker(source_name).iter_errors(json_view(doc))]


# ======================================================================
# THE TRANSLATION
# ======================================================================

def test_both_published_contracts_translate():
    """Neither committed contract holds a construct the translation refuses."""
    for source_name in PUBLISHED_CONTRACTS:
        validator = validator_for(source_name)
        assert set(validator) == {"$jsonSchema"}
        assert validator["$jsonSchema"]["bsonType"] == "object"


def test_translation_names_an_integer_as_the_bson_types_it_lands_as():
    """MongoDB's `type` rejects "integer" outright, and a Python integer is an
    int32 when it fits and an int64 when it does not, so both are named."""
    schema = validator_for("providermedicare")["$jsonSchema"]
    assert schema["properties"]["record_count"]["bsonType"] == ["int", "long"]


def test_translation_emits_no_type_keyword_anywhere():
    """MongoDB will not accept `type` beside `bsonType` in one subschema, so
    the translation restates every contract type and emits none."""
    text = json.dumps(validator_for("providermedicare"))
    assert '"type"' not in text
    text = json.dumps(validator_for("specialtymedicare"))
    assert '"type"' not in text


def test_translation_drops_only_the_keywords_that_carry_no_constraint():
    """$schema names the dialect and $id names the contract; neither
    constrains a document, and MongoDB rejects both."""
    out = to_mongo_validator(
        {"$schema": "https://json-schema.org/draft/2020-12/schema",
         "$id": "https://dev.chathealthy.ai/schemas/X.json",
         "$defs": {"unused": {"type": "string"}},
         "type": "object",
         "required": ["a"],
         "properties": {"a": {"type": "string"}}},
        contract_name="X.json")["$jsonSchema"]
    assert "$schema" not in out
    assert "$id" not in out
    assert "$defs" not in out
    assert out == {"bsonType": "object", "required": ["a"],
                   "properties": {"a": {"bsonType": "string"}}}


def test_translation_preserves_required_and_properties():
    schema = validator_for("providermedicare")["$jsonSchema"]
    contract = load_contract(PUBLISHED_CONTRACTS["providermedicare"])
    assert schema["required"] == contract["required"]
    assert set(schema["properties"]) == set(contract["properties"])
    assert schema["additionalProperties"] is False


def test_translation_recurses_into_a_nested_object():
    """_id is an object of its own, with its own required list and pattern."""
    inner = validator_for("providermedicare")["$jsonSchema"]["properties"]["_id"]
    assert inner["bsonType"] == "object"
    assert inner["required"] == ["npi", "molecule_id"]
    assert inner["additionalProperties"] is False
    assert inner["properties"]["molecule_id"]["minLength"] == 1
    assert inner["properties"]["npi"]["pattern"]


def test_translation_recurses_into_array_items():
    """molecules is a map of objects four levels deep in the specialty
    contract; every level must come through restated."""
    schema = validator_for("specialtymedicare")["$jsonSchema"]
    item = schema["properties"]["molecules"]["additionalProperties"]
    assert item["bsonType"] == "object"
    stat = item["properties"]["specialty_stats"]["properties"]["tot_clms"]
    assert stat["bsonType"] == "object"
    assert stat["required"] == ["max", "min", "median", "avg"]
    # A count's median divides on an even-length set, so it is an integer or
    # an exact decimal; the string form is the JSON view of that decimal and
    # is constrained to decimal digits.
    assert stat["properties"]["median"]["bsonType"] == [
        "int", "long", "decimal", "null"]
    assert stat["properties"]["max"]["bsonType"] == ["int", "long", "null"]


def test_translation_recurses_into_a_list_of_item_schemas():
    out = to_mongo_validator(
        {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]},
        contract_name="X.json")["$jsonSchema"]
    assert out["items"] == [{"bsonType": "string"},
                            {"bsonType": ["int", "long"]}]


def test_translation_refuses_a_ref_rather_than_dropping_the_constraint():
    """MongoDB cannot follow a reference. Dropping one would leave a validator
    that permits what the contract forbids, so it raises instead."""
    with pytest.raises(ChatHealthyException) as caught:
        to_mongo_validator(
            {"type": "object",
             "properties": {"a": {"$ref": "#/$defs/thing"}},
             "$defs": {"thing": {"type": "string"}}},
            contract_name="Refs.json")
    assert caught.value.mode == "not_implemented"
    assert "$ref" in caught.value.message
    assert "Refs.json" in caught.value.message
    assert "properties.a.$ref" in caught.value.message


def test_translation_refuses_an_unknown_keyword_and_names_where_it_is():
    with pytest.raises(ChatHealthyException) as caught:
        to_mongo_validator(
            {"type": "object",
             "properties": {"a": {"type": "string", "format": "email"}}},
            contract_name="Formats.json")
    assert caught.value.mode == "not_implemented"
    assert "format" in caught.value.message
    assert "Formats.json" in caught.value.message
    assert caught.value.context["location"] == "properties.a.format"


def test_translation_refuses_a_composition_keyword():
    """anyOf has no restatement here. It is MongoDB-supported but untried, so
    it fails loudly rather than being emitted untested."""
    with pytest.raises(ChatHealthyException) as caught:
        to_mongo_validator({"anyOf": [{"type": "string"}]},
                           contract_name="AnyOf.json")
    assert "anyOf" in caught.value.message


def test_translation_refuses_a_type_name_it_cannot_restate():
    with pytest.raises(ChatHealthyException) as caught:
        to_mongo_validator({"type": "timestamp"}, contract_name="Odd.json")
    assert caught.value.mode == "not_implemented"
    assert "timestamp" in caught.value.message


def test_a_missing_contract_file_is_a_configuration_fatal():
    with pytest.raises(ChatHealthyException) as caught:
        load_contract("ChatHealthyNoSuchContractSchema.json")
    assert caught.value.mode == "config_error"


def test_an_undeclared_collection_has_no_validator():
    with pytest.raises(ChatHealthyException) as caught:
        validator_for("medicare_partd")
    assert caught.value.mode == "value_error"


def test_all_three_published_collections_declare_a_contract():
    # Every collection this pipeline publishes carries its contract as its
    # validator, the FDA molecule collection included.
    assert set(PUBLISHED_CONTRACTS) == {
        "providermedicare", "specialtymedicare", "fda_drug"}
    for source in PUBLISHED_CONTRACTS:
        assert validator_for(source)["$jsonSchema"]["bsonType"] == "object"


# ======================================================================
# THE TRANSLATED VALIDATOR STILL BITES
# ======================================================================

def _row(npi="1003000126", generic="ATORVASTATIN CALCIUM"):
    return {C["npi"]: npi, C["state"]: "FL", C["brand"]: "LIPITOR",
            C["generic"]: generic, C["total_claims"]: 11,
            C["total_30day_fills"]: 12, C["days_supply"]: 300,
            C["total_drug_cost"]: 250.5, C["beneficiary_count"]: 15,
            C["bene_sprsn_flag"]: ""}


def _built_provider_record() -> dict:
    """Built by the engine's own pure functions, not hand-written."""
    mid = molecule_id("ATORVASTATIN CALCIUM")
    rows = [_row(), _row()]
    return record_document("1003000126", mid, fold_rows(rows), "FL")


def _built_specialty_record() -> dict:
    """Assembled exactly as rollup_specialtymedicare assembles it."""
    rows = [_row(), _row(npi="1104000127")]
    acc = accumulate_provider_molecule_totals(rows)
    by_nucc = rollup_specialty_v83(acc, {"1003000126": ["207Q00000X"],
                                         "1104000127": ["207Q00000X"]})
    nucc = "207Q00000X"
    return {
        "_id": nucc,
        "nucc_code": nucc,
        # keyed by molecule: the rollup fans out by molecule and each
        # worker writes only the keys it owns
        "molecules": {mid: by_nucc[nucc][mid] for mid in by_nucc[nucc]},
        "provider_count": 2,
        "provenance": {
            "aggregated_from": ["StagingMedicarePartD_v_1", "Provider_v_5"],
            "run_id": "run-x",
            "built_at": datetime.datetime.utcnow().isoformat()},
    }


def test_a_built_provider_record_satisfies_the_translated_validator():
    assert _errors("providermedicare", _built_provider_record()) == []


def test_a_built_specialty_record_satisfies_the_translated_validator():
    assert _errors("specialtymedicare", _built_specialty_record()) == []


def test_the_translated_validator_refuses_a_record_missing_a_required_field():
    doc = _built_provider_record()
    del doc["record_count"]
    assert _errors("providermedicare", doc)


def test_the_translated_validator_refuses_an_undeclared_field():
    doc = _built_provider_record()
    doc["invented_by_a_future_step"] = 1
    assert _errors("providermedicare", doc)


def test_the_translated_validator_refuses_a_count_that_is_not_an_integer():
    doc = _built_provider_record()
    doc["record_count"] = "2"
    assert _errors("providermedicare", doc)


def test_the_translated_validator_refuses_a_value_outside_an_enum():
    doc = _built_provider_record()
    doc["medicare_population"] = "Part B"
    assert _errors("providermedicare", doc)


def test_the_translated_validator_refuses_an_npi_that_breaks_the_pattern():
    doc = _built_provider_record()
    doc["npi"] = "100319"
    assert _errors("providermedicare", doc)


def test_the_translated_validator_refuses_a_size_tier_out_of_range():
    doc = _built_provider_record()
    doc["size_tier"] = 9
    assert _errors("providermedicare", doc)


def test_the_translated_validator_refuses_a_specialty_record_without_provenance():
    doc = _built_specialty_record()
    del doc["provenance"]
    assert _errors("specialtymedicare", doc)


def test_the_translated_validator_refuses_a_malformed_nested_statistic():
    doc = _built_specialty_record()
    first = sorted(doc["molecules"])[0]
    doc["molecules"][first]["specialty_stats"]["tot_clms"]["median"] = "middle"
    assert _errors("specialtymedicare", doc)


# ======================================================================
# A REFUSED WRITE IS A FINDING
# ======================================================================

def _refusal(index: int, key_hint: str) -> dict:
    """One writeErrors entry shaped as MongoDB returns a validator refusal."""
    return {"index": index, "code": DOCUMENT_FAILED_VALIDATION,
            "errmsg": f"Document failed validation ({key_hint})",
            "errInfo": {"failingDocumentId": key_hint}}


class _Findings:
    """Collects record_discrepancy calls. A callback, not a collection."""

    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _Refused(Exception):
    """Stands for pymongo's BulkWriteError only as the carried original; the
    details dict under test is the real shape pymongo hands over."""

    def __init__(self, details):
        super().__init__("refused")
        self.details = details


def test_the_finding_class_is_one_the_configuration_already_declares():
    """A class the configuration does not name is rejected at write time, so
    the refusal must use a declared one and never invent its own."""
    config = json.loads(_CONFIG.read_text(encoding="utf-8"))
    for record in config["pipeline_config"]:
        declared = record["discrepancy_report"]["finding_types"]
        assert STORE_REFUSAL_FINDING in declared, record["env"]


def test_every_refused_document_becomes_its_own_finding():
    """Not one finding for the batch. One per document the store refused."""
    keys = ["1003000126:m1", "1104000127:m2", "1215000128:m3",
            "1326000129:m4"]
    details = {"writeErrors": [_refusal(1, "b"), _refusal(3, "d")],
               "nInserted": 2}
    findings = _Findings()
    refused = record_refused_writes(
        details=details, keys=keys, artifact="providermedicare",
        step="build_providermedicare_base", record_discrepancy=findings,
        exception=_Refused(details))
    assert refused == 2
    assert len(findings.calls) == 2
    assert [c["record_key"] for c in findings.calls] == ["1104000127:m2",
                                                         "1326000129:m4"]


def test_a_provider_finding_carries_the_npi_and_the_molecule():
    keys = [record_key(_built_provider_record())]
    details = {"writeErrors": [_refusal(0, "a")]}
    findings = _Findings()
    record_refused_writes(
        details=details, keys=keys, artifact="providermedicare",
        step="build_providermedicare_base", record_discrepancy=findings,
        exception=_Refused(details))
    call = findings.calls[0]
    assert call["record_key"] == "1003000126:" + molecule_id("ATORVASTATIN CALCIUM")
    assert call["reason"] == STORE_REFUSAL_FINDING
    assert call["artifact"] == "providermedicare"
    assert call["step"] == "build_providermedicare_base"
    assert call["detail"]["kind"] == "store_refused_write"
    assert call["detail"]["code"] == DOCUMENT_FAILED_VALIDATION
    assert "Document failed validation" in call["detail"]["store_message"]


def test_a_specialty_finding_carries_the_nucc_code():
    details = {"writeErrors": [_refusal(0, "207Q00000X")]}
    findings = _Findings()
    record_refused_writes(
        details=details, keys=["207Q00000X"], artifact="specialtymedicare",
        step="rollup_specialtymedicare", record_discrepancy=findings,
        exception=_Refused(details))
    assert findings.calls[0]["record_key"] == "207Q00000X"
    assert findings.calls[0]["artifact"] == "specialtymedicare"


def test_a_batch_with_no_write_errors_records_nothing():
    findings = _Findings()
    assert record_refused_writes(
        details={"nInserted": 3}, keys=["a", "b", "c"],
        artifact="providermedicare", step="enrich",
        record_discrepancy=findings, exception=_Refused({})) == 0
    assert findings.calls == []


def test_a_write_error_that_is_not_a_contract_refusal_is_split_out():
    details = {"writeErrors": [_refusal(0, "a"),
                               {"index": 1, "code": 11000,
                                "errmsg": "E11000 duplicate key"}]}
    refusals, others = refusals_and_other_errors(details, ["a", "b"])
    assert [r["record_key"] for r in refusals] == ["a"]
    assert [o["code"] for o in others] == [11000]


def test_a_write_error_that_is_not_a_contract_refusal_is_converted_and_raised():
    """The raw pymongo error never escapes as itself, and a failure the
    contract does not account for is not quietly filed as a finding."""
    details = {"writeErrors": [{"index": 0, "code": 11000,
                                "errmsg": "E11000 duplicate key"}]}
    original = _Refused(details)
    findings = _Findings()
    with pytest.raises(ChatHealthyException) as caught:
        record_refused_writes(
            details=details, keys=["a"], artifact="providermedicare",
            step="enrich", record_discrepancy=findings, exception=original)
    assert caught.value.mode == "runtime_error"
    assert caught.value.exception is original
    assert "11000" in caught.value.message
    assert findings.calls == []


def test_an_index_outside_the_batch_leaves_the_key_unknown_rather_than_guessed():
    refusals, others = refusals_and_other_errors(
        {"writeErrors": [_refusal(7, "x")]}, ["a", "b"])
    assert others == []
    assert refusals[0]["record_key"] is None
    assert refusals[0]["batch_index"] == 7
