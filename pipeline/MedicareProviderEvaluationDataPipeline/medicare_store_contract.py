# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Store-enforced record contracts (EPIC-010-F-001-S-012-REQ-B-002,
LLD v83 I.3.12).

CONFORMANCE IS ENFORCED BY THE STORE, NOT BY THE CALLERS. Each published
collection carries its normative JSON Schema installed on the collection
itself as its MongoDB validator, so the database refuses a write that does not
conform. Two things live here: the translation from a published contract into
the validator MongoDB will accept, and the reading of a refusal back into a
per-record finding.

WHY A TRANSLATION IS NEEDED. MongoDB's $jsonSchema is not JSON Schema 2020-12.
It is a subset with its own type vocabulary: `type` may not say "integer" at
all, and `bsonType` is the keyword that can, naming the concrete BSON types
("int", "long") a Python integer actually lands as. So every contract type is
restated as a bsonType and nothing emits `type`, which MongoDB will not accept
beside `bsonType` in the same subschema.

WHAT THE TRANSLATION REFUSES TO GUESS. A validator that silently permits what
the contract forbids is worse than no validator, because it reads as proof and
is not. So the translation knows three sets of keywords and nothing else:
keywords it restates, keywords it passes through unchanged, and two keywords
that carry no constraint at the place they appear ($schema and $id, which name
the dialect and the contract, and $defs, which is a container of definitions
reached only through $ref). Every other keyword RAISES, naming the keyword and
the contract, including $ref: MongoDB cannot follow a reference, so dropping
one would drop the constraint it stands for without saying so. Adding a
keyword to this module is how a contract construct becomes enforceable, and a
contract that grows one fails loudly here rather than quietly in the store.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from bson.decimal128 import Decimal128
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

# The same directory, reached the same way, that every other module in this
# package reaches the committed contracts by: the repository's Website/schemas,
# which the worker image materializes at /app/Website/schemas (see
# Dockerfile.medicare-worker). This file sits in the package root, so the
# repository root is two parents up both in the tree and in the image.
_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "Website" / "schemas"

# The published collections whose contract the store enforces, by the source
# name the dataset registry knows them under.
PUBLISHED_CONTRACTS = {
    "providermedicare": "ChatHealthyProviderMedicareSchema.json",
    "specialtymedicare": "ChatHealthySpecialtyMedicareSchema.json",
    "fda_drug": "ChatHealthyFdaProductToDiagnosisResolutionSchema.json",
}

# A contract type restated as the BSON types a value of that type lands as.
# "integer" is the one that cannot be passed through: MongoDB's `type` rejects
# it outright, and an integer written by pymongo is an int32 when it fits and
# an int64 when it does not, so both are named.
_BSON_TYPE_OF = {
    "object": ("object",),
    "array": ("array",),
    "string": ("string",),
    "boolean": ("bool",),
    "null": ("null",),
    "number": ("number",),
    "integer": ("int", "long"),
}

# Keywords that mean the same thing to MongoDB and are copied unchanged.
_PASSED_THROUGH = frozenset((
    "required", "enum", "pattern", "minimum", "maximum", "minLength",
    "title", "description",
))

# Keywords that constrain nothing where they appear. $schema names the
# dialect, $id names the contract, and $defs is only reachable through $ref --
# which raises, so a referenced definition can never be lost silently.
_CARRIES_NO_CONSTRAINT = frozenset(("$schema", "$id", "$defs"))


def json_view(value: Any) -> Any:
    """A record as the published JSON contract describes it.

    One field is held three ways and the contract describes the outermost:
    Python carries an exact value as decimal.Decimal, BSON carries it as
    Decimal128, and JSON has no decimal type at all, so the contract declares
    those fields strings. This renders the JSON view -- every exact value as
    its own digits in a string -- which is what a JSON Schema validator reads
    and what a JSON consumer receives.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Decimal128):
        return str(value.to_decimal())
    if isinstance(value, dict):
        return {k: json_view(v) for k, v in value.items()}
    if isinstance(value, list):
        return [json_view(v) for v in value]
    return value


def contract_path(file_name: str) -> Path:
    return _SCHEMA_DIR / file_name


def load_contract(file_name: str) -> dict:
    """The published contract as committed, read from the schema directory."""
    path = contract_path(file_name)
    if not path.is_file():
        raise ChatHealthyException(
            mode="config_error",
            component="medicare_store_contract",
            message=(f"published contract {file_name!r} is not present at "
                     f"{path}; the collection's validator cannot be installed "
                     f"without the contract it enforces"),
            contract=file_name,
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _untranslatable(keyword: str, contract_name: str, path: str) -> None:
    """Refuse a keyword this translation cannot restate.

    Raises rather than returning the exception: a factory that returns one
    reads, at the call site, as a raise of something that is not a
    ChatHealthyException, and the shape of a deliberate raise should be
    obvious to a reader and to the scanner alike.
    """
    raise ChatHealthyException(
        mode="not_implemented",
        component="medicare_store_contract",
        message=(f"contract {contract_name!r} uses keyword {keyword!r} at "
                 f"{path or '(root)'}, which this translation cannot restate "
                 f"for MongoDB's $jsonSchema. Dropping it would leave a "
                 f"validator that permits what the contract forbids, so the "
                 f"keyword must be translated here before the contract can be "
                 f"enforced by the store"),
        contract=contract_name,
        keyword=keyword,
        location=path or "(root)",
    )


def _translate(node: Any, *, contract_name: str, path: str) -> dict:
    """One subschema, restated for MongoDB's $jsonSchema."""
    if not isinstance(node, dict):
        _untranslatable(f"<{type(node).__name__} in place of a subschema>",
                        contract_name, path)
    out: dict[str, Any] = {}
    for keyword, value in node.items():
        here = f"{path}.{keyword}" if path else keyword
        if keyword in _CARRIES_NO_CONSTRAINT:
            continue
        if keyword in _PASSED_THROUGH:
            out[keyword] = value
        elif keyword == "bsonType":
            # Already MongoDB's own vocabulary; nothing to restate.
            out["bsonType"] = value
        elif keyword == "type":
            out["bsonType"] = _bson_type(value, contract_name, here)
        elif keyword == "const":
            # MongoDB has no const, and enum of one value is exactly it: the
            # same single permitted value, enforced identically. This is a
            # restatement, not a relaxation.
            out["enum"] = [value]
        elif keyword == "properties":
            if not isinstance(value, dict):
                _untranslatable("properties (not an object)",
                                contract_name, here)
            out["properties"] = {
                name: _translate(sub, contract_name=contract_name,
                                 path=f"{here}.{name}")
                for name, sub in value.items()
            }
        elif keyword == "items":
            if isinstance(value, list):
                out["items"] = [
                    _translate(sub, contract_name=contract_name,
                               path=f"{here}[{index}]")
                    for index, sub in enumerate(value)
                ]
            else:
                out["items"] = _translate(value, contract_name=contract_name,
                                          path=here)
        elif keyword == "additionalProperties":
            if isinstance(value, bool):
                out["additionalProperties"] = value
            else:
                out["additionalProperties"] = _translate(
                    value, contract_name=contract_name, path=here)
        else:
            _untranslatable(keyword, contract_name, here)
    return out


def _bson_type(value: Any, contract_name: str, path: str) -> Any:
    """A contract `type` as a MongoDB `bsonType`."""
    names = value if isinstance(value, list) else [value]
    resolved: list[str] = []
    for name in names:
        mapped = _BSON_TYPE_OF.get(name) if isinstance(name, str) else None
        if mapped is None:
            _untranslatable(f"type: {name!r}", contract_name, path)
        for bson_name in mapped:
            if bson_name not in resolved:
                resolved.append(bson_name)
    if len(resolved) == 1:
        return resolved[0]
    return resolved


def to_mongo_validator(contract: dict, *, contract_name: str) -> dict:
    """The collection validator that enforces this published contract.

    Returns the document MongoDB takes as its `validator`: one $jsonSchema
    restating the contract. Pure -- it reads no collection and opens no
    connection.
    """
    return {"$jsonSchema": _translate(contract, contract_name=contract_name,
                                      path="")}


def validator_for(source_name: str) -> dict:
    """The validator for one published collection, by its source name."""
    file_name = PUBLISHED_CONTRACTS.get(source_name)
    if file_name is None:
        raise ChatHealthyException(
            mode="value_error",
            component="medicare_store_contract",
            message=(f"no published contract is declared for source "
                     f"{source_name!r}; the collections whose contract the "
                     f"store enforces are "
                     f"{sorted(PUBLISHED_CONTRACTS)}"),
            source_name=source_name,
        )
    return to_mongo_validator(load_contract(file_name),
                              contract_name=file_name)


# ---------------------------------------------------------------------------
# A refused write is a finding, not an error.
# ---------------------------------------------------------------------------

# MongoDB's code for a write the collection's validator refused.
DOCUMENT_FAILED_VALIDATION = 121

# The finding class a refusal is recorded under. Declared already in
# pipeline_config.json discrepancy_report.finding_types; a class the
# configuration does not name is rejected at write time, so this is not a
# vocabulary this module may extend.
STORE_REFUSAL_FINDING = "schema_violation"


def refusals_and_other_errors(
    details: dict | None,
    keys: list[str],
) -> tuple[list[dict], list[dict]]:
    """Split a BulkWriteError's details into contract refusals and the rest.

    `details` is the plain dict pymongo carries on a BulkWriteError; `keys` is
    the business key of each document in the batch, in the order the batch was
    submitted, so a write error's `index` names the record that was refused.
    One entry per refused DOCUMENT -- never one for the batch.

    A refusal is code 121, the collection validator saying no. Anything else
    in the same batch -- a duplicate key, a lost connection -- is not a
    contract matter and is returned separately for the caller to convert.
    """
    refusals: list[dict] = []
    others: list[dict] = []
    for error in (details or {}).get("writeErrors") or []:
        index = error.get("index")
        key = None
        if isinstance(index, int) and 0 <= index < len(keys):
            key = keys[index]
        entry = {
            "record_key": key,
            "batch_index": index,
            "code": error.get("code"),
            "store_message": error.get("errmsg"),
        }
        if error.get("code") == DOCUMENT_FAILED_VALIDATION:
            refusals.append(entry)
        else:
            others.append(entry)
    return refusals, others


def record_refused_writes(
    *,
    details: dict | None,
    keys: list[str],
    artifact: str,
    step: str,
    record_discrepancy,
    exception: Exception,
) -> int:
    """Record one finding per document the store refused; return how many.

    The raw pymongo error never leaves the write path as itself. Every
    document the validator refused becomes an ordinary per-record finding
    carrying that record's business key, under the controlled vocabulary, and
    the documents that conformed stay written -- a batch is not abandoned
    because one of its documents failed the contract. Any write error that is
    NOT a contract refusal is converted and raised, carrying the original.
    """
    refusals, others = refusals_and_other_errors(details, keys)
    if others:
        raise ChatHealthyException(
            mode="runtime_error",
            component=step,
            message=(f"{step}: {len(others)} write error(s) against {artifact} "
                     f"that the collection's contract does not account for; "
                     f"codes {sorted({str(o['code']) for o in others})}"),
            exception=exception,
            write_errors=others,
        )
    for entry in refusals:
        record_discrepancy(
            reason=STORE_REFUSAL_FINDING,
            step=step,
            artifact=artifact,
            record_key=entry["record_key"],
            detail={"kind": "store_refused_write", **entry},
        )
    _log.LogPipeline(
        "INFO", "%s: the store refused %d document(s) of %s; each is a "
        "per-record %s finding and the batch's conforming documents were "
        "written", step, len(refusals), artifact, STORE_REFUSAL_FINDING)
    return len(refusals)
