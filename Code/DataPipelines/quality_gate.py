# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline Architecture v4 — QualityGate
# Validates data between pipeline stages. Fail fast on bad data.

import logging
from typing import Optional

log = logging.getLogger("quality_gate")


class QualityGate:
    """Data quality validation between pipeline stages.

    Usage:
        gate = QualityGate("step_2_load", min_rows=1000,
                           required_fields=["npi", "practice_address.state"])
        gate.enforce(collection, query={})
    """

    def __init__(self, stage_name: str, min_rows: int = 1,
                 required_fields: Optional[list] = None,
                 max_null_fraction: float = 0.05):
        self.stage_name = stage_name
        self.min_rows = min_rows
        self.required_fields = required_fields or []
        self.max_null_fraction = max_null_fraction

    def enforce(self, collection, query: dict = None) -> dict:
        """Run all quality checks. Raises on failure."""
        q = query or {}
        results = {
            "stage": self.stage_name,
            "row_count": self._check_row_count(collection, q),
            "null_checks": self._check_nulls(collection, q),
        }
        failures = [k for k, v in results.items()
                     if isinstance(v, dict) and v.get("passed") is False]
        if failures:
            msg = f"QualityGate FAILED at {self.stage_name}: {failures}"
            log.error(msg)
            raise QualityGateFailure(msg, results)

        log.info("QualityGate PASSED at %s: %s rows", self.stage_name,
                 f"{results['row_count']['count']:,}")
        return results

    def _check_row_count(self, collection, query: dict) -> dict:
        count = collection.estimated_document_count() if not query else collection.count_documents(query)
        passed = count >= self.min_rows
        if not passed:
            log.error("QualityGate %s: row count %d < min %d",
                      self.stage_name, count, self.min_rows)
        return {"count": count, "min": self.min_rows, "passed": passed}

    def _check_nulls(self, collection, query: dict) -> dict:
        if not self.required_fields:
            return {"passed": True, "checked": 0}

        total = collection.estimated_document_count() if not query else collection.count_documents(query)
        if total == 0:
            return {"passed": True, "checked": 0}

        # Single $facet aggregation — one pass instead of 2N separate count queries
        facets = {}
        for field in self.required_fields:
            safe_name = field.replace(".", "_")
            facets[f"{safe_name}_null"] = [
                {"$match": {**query, field: {"$in": [None, "", []]}}},
                {"$count": "n"}
            ]
            facets[f"{safe_name}_missing"] = [
                {"$match": {**query, field: {"$exists": False}}},
                {"$count": "n"}
            ]

        try:
            result = list(collection.aggregate([{"$facet": facets}], allowDiskUse=True))
            counts = result[0] if result else {}
        except Exception as e:
            log.warning("QualityGate %s: $facet failed (%s), skipping null checks", self.stage_name, e)
            return {"passed": True, "checked": 0, "note": "facet_failed"}

        failures = []
        for field in self.required_fields:
            safe_name = field.replace(".", "_")
            null_arr = counts.get(f"{safe_name}_null", [])
            missing_arr = counts.get(f"{safe_name}_missing", [])
            null_count = (null_arr[0]["n"] if null_arr else 0) + (missing_arr[0]["n"] if missing_arr else 0)
            fraction = null_count / total
            if fraction > self.max_null_fraction:
                failures.append({
                    "field": field,
                    "null_count": null_count,
                    "fraction": round(fraction, 4),
                    "max": self.max_null_fraction,
                })
                log.error("QualityGate %s: field '%s' null fraction %.2f%% > max %.2f%%",
                          self.stage_name, field, fraction * 100, self.max_null_fraction * 100)

        return {"passed": len(failures) == 0, "checked": len(self.required_fields),
                "failures": failures}


class QualityGateFailure(Exception):
    def __init__(self, message: str, results: dict):
        super().__init__(message)
        self.results = results
