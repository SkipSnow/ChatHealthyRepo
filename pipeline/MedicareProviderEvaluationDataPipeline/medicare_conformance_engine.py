# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Record-contract conformance engine (EPIC-010-F-007 / F-001-S-012).

Validates every published ProviderMedicare, SpecialtyMedicare and
fdaProductToDiagnosisResolution record against its JSON Schema contract. A single nonconforming
record stops the run (the collection it builds is normative); the engine reports
the first offenders per collection so the drift is actionable.
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


def check_conformance(*, run_id: str, collections: dict, cms_partd_row_count: int | None = None,
                      sample_offenders: int = 10) -> dict[str, Any]:
    """collections: source_name -> pymongo collection. Validate every doc against
    its JSON Schema, and -- when cms_partd_row_count is given -- enforce the
    conformance invariant that the sum of record_count over every provider x
    molecule document equals the CMS Part D row count (every CMS row represented
    exactly once). Either failure stops the run."""
    report: dict[str, Any] = {}
    total_nonconforming = 0
    provider_record_count_sum = 0
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
                provider_record_count_sum += int(doc.get("record_count") or 0)
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

    invariant: dict[str, Any] | None = None
    if cms_partd_row_count is not None:
        delta = provider_record_count_sum - cms_partd_row_count
        invariant = {"sum_record_count": provider_record_count_sum,
                     "cms_partd_rows": cms_partd_row_count, "delta": delta}
        _log.LogPipeline(
            "INFO", "conformance invariant: sum_record_count=%d cms_rows=%d delta=%d",
            provider_record_count_sum, cms_partd_row_count, delta)
        if delta != 0:
            raise ChatHealthyException(
                mode="runtime_error",
                component="medicare_contract_conformance",
                message=(f"medicare_contract_conformance: Sum(record_count)={provider_record_count_sum} "
                         f"!= CMS Part D rows={cms_partd_row_count} (delta {delta}); fewer is a drop, "
                         "more is a duplicate"),
                report=report, invariant=invariant)

    if total_nonconforming:
        raise ChatHealthyException(
            mode="runtime_error",
            component="medicare_contract_conformance",
            message=f"medicare_contract_conformance: {total_nonconforming} nonconforming record(s)",
            report=report,
        )
    return {"conformant": True, "report": report, "invariant": invariant}
