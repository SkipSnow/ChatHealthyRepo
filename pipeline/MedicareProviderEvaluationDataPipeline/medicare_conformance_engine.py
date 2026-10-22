# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Record-contract conformance engine (EPIC-010-F-007 / F-001-S-012,
LLD v83 III.6 with the III.7 hard-fail semantics, operator-ruled 2026-10-20).

Three checks, each with its own ruled consequence:

  H1 -- Part D exactly-once arithmetic:
        Sum(total_cms_record_count over molecule masters) + the no-molecule
        bucket == the run's staged Part D row count. A mismatch is a FINDING
        with the delta -- operationally visible, never a run kill.
  H2 -- FDA fold arithmetic:
        Sum(len(set_ids) over molecule records) == the run's in-scope label
        count, and the transient label-result count matches. A mismatch is a
        FINDING; unfolded labels were orphan-stamped by the finalize step.
  H3 -- JSON-Schema validation of every published record: a violation means
        the software wrote a non-compliant record -- a software bug and a
        HARD FAIL.

Version-scoped by construction: the collections validated are the run's own
data_version collections (resolved by the caller through the registry), and
the pre-pass cleared them at build start (full annual replace), so no residue
can pollute the arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

_SCHEMA_DIR = Path(__file__).resolve().parents[2] / "Website" / "schemas"
_CONTRACTS = {
    "providermedicare": "ChatHealthyProviderMedicareSchema.json",
    "specialtymedicare": "ChatHealthySpecialtyMedicareSchema.json",
    "fda_drug": "ChatHealthyFdaProductToDiagnosisResolutionSchema.json",
}


def _validator(schema_file: str):
    from jsonschema import Draft202012Validator
    schema = json.loads((_SCHEMA_DIR / schema_file).read_text(encoding="utf-8"))
    return Draft202012Validator(schema)


def check_conformance(*, collections: dict,
                      cms_partd_row_count: int | None = None,
                      no_molecule_rows: int = 0,
                      fda_in_scope_labels: int | None = None,
                      record_discrepancy=None,
                      sample_offenders: int = 10) -> dict[str, Any]:
    """collections: source_name -> pymongo collection (the run's versioned
    collections). Runs the three III.6 checks; only H3 (schema violation)
    raises. Arithmetic mismatches are recorded as findings through
    record_discrepancy and returned in the report."""
    report: dict[str, Any] = {}
    total_nonconforming = 0
    master_record_count_sum = 0
    fda_set_ids_sum = 0
    for source_name, coll in collections.items():
        schema_file = _CONTRACTS.get(source_name)
        if not schema_file:
            continue
        validator = _validator(schema_file)
        checked = nonconforming = 0
        offenders: list[dict] = []
        for doc in coll.find({}):
            checked += 1
            if source_name == "providermedicare":
                master_record_count_sum += int(doc.get("total_cms_record_count") or 0)
            elif source_name == "fda_drug":
                fda_set_ids_sum += len(doc.get("set_ids") or [])
            errors = sorted(validator.iter_errors(doc), key=lambda e: e.path)
            if errors:
                nonconforming += 1
                if len(offenders) < sample_offenders:
                    offenders.append({
                        "_id": str(doc.get("_id")),
                        "error": f"{list(errors[0].path)}: {errors[0].message}",
                    })
        total_nonconforming += nonconforming
        report[source_name] = {
            "checked": checked, "nonconforming": nonconforming, "offenders": offenders,
        }
        _log.LogPipeline(
            "INFO", "conformance[%s]: checked=%d nonconforming=%d",
            source_name, checked, nonconforming,
        )

    # H1 -- Part D exactly-once arithmetic: a mismatch is a FINDING, not a kill.
    partd_arithmetic: dict[str, Any] | None = None
    if cms_partd_row_count is not None:
        represented = master_record_count_sum + int(no_molecule_rows)
        delta = represented - cms_partd_row_count
        partd_arithmetic = {
            "sum_total_cms_record_count": master_record_count_sum,
            "no_molecule_rows": int(no_molecule_rows),
            "cms_partd_rows": cms_partd_row_count,
            "delta": delta,
        }
        _log.LogPipeline(
            "INFO",
            "conformance Part D arithmetic: masters=%d + bucket=%d vs rows=%d delta=%d",
            master_record_count_sum, no_molecule_rows, cms_partd_row_count, delta)
        if delta != 0 and record_discrepancy is not None:
            record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="medicare_contract_conformance", artifact="providermedicare",
                record_key="partd_exactly_once",
                detail={"kind": "partd_arithmetic_mismatch", **partd_arithmetic,
                        "note": "fewer is a drop, more is a duplicate (H1: "
                                "finding, never a run kill)"})

    # H2 -- FDA fold arithmetic: a mismatch is a FINDING; orphans were stamped.
    fda_arithmetic: dict[str, Any] | None = None
    if fda_in_scope_labels is not None:
        delta = fda_set_ids_sum - fda_in_scope_labels
        fda_arithmetic = {
            "sum_set_ids": fda_set_ids_sum,
            "in_scope_labels": fda_in_scope_labels,
            "delta": delta,
        }
        _log.LogPipeline(
            "INFO", "conformance FDA arithmetic: set_ids=%d vs in_scope=%d delta=%d",
            fda_set_ids_sum, fda_in_scope_labels, delta)
        if delta != 0 and record_discrepancy is not None:
            record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="medicare_contract_conformance", artifact="fda",
                record_key="fda_fold_exactly_once",
                detail={"kind": "fda_arithmetic_mismatch", **fda_arithmetic,
                        "note": "unfolded labels carry orphan flags in the "
                                "transient (H2: finding, never a run kill)"})

    # H3 -- schema violation: a software bug; HARD FAIL.
    if total_nonconforming:
        raise ChatHealthyException(
            mode="runtime_error",
            component="medicare_contract_conformance",
            message=(f"medicare_contract_conformance: {total_nonconforming} "
                     f"nonconforming record(s) -- writing non-compliant records "
                     f"is a software bug (hard-fail H3)"),
            report=report,
        )
    return {"conformant": True, "report": report,
            "partd_arithmetic": partd_arithmetic,
            "fda_arithmetic": fda_arithmetic}
