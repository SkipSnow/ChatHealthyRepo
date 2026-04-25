# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""QualityGate tests — PIPE-QG-001"""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from quality_gate import QualityGate, QualityGateFailure

# Use a simple list-of-dicts mock collection for unit testing
class MockCollection:
    def __init__(self, docs):
        self._docs = docs

    def estimated_document_count(self):
        return len(self._docs)

    def count_documents(self, query):
        if not query:
            return len(self._docs)
        count = 0
        for doc in self._docs:
            match = True
            for k, v in query.items():
                if isinstance(v, dict):
                    if "$in" in v and doc.get(k) not in v["$in"]:
                        match = False
                    if "$exists" in v:
                        if v["$exists"] and k not in doc:
                            match = False
                        if not v["$exists"] and k in doc:
                            match = False
                elif doc.get(k) != v:
                    match = False
            if match:
                count += 1
        return count

    def aggregate(self, pipeline, **kwargs):
        """Minimal $facet aggregation mock for null-check tests."""
        if not pipeline or "$facet" not in pipeline[0]:
            return []
        facets = pipeline[0]["$facet"]
        result = {}
        for facet_name, stages in facets.items():
            # Each facet is [$match, $count]
            match_query = stages[0].get("$match", {})
            matched = 0
            for doc in self._docs:
                ok = True
                for k, v in match_query.items():
                    if isinstance(v, dict):
                        if "$in" in v and doc.get(k) not in v["$in"]:
                            ok = False
                        if "$exists" in v:
                            if v["$exists"] and k not in doc:
                                ok = False
                            if not v["$exists"] and k in doc:
                                ok = False
                    elif doc.get(k) != v:
                        ok = False
                if ok:
                    matched += 1
            result[facet_name] = [{"n": matched}] if matched > 0 else []
        return [result]

def test_min_row_count_fail():
    """EPIC-006-F-011-S-001-REQ-T-001: must raise when count < min_rows"""
    coll = MockCollection([{"npi": "1"}])
    gate = QualityGate("test", min_rows=100)
    with pytest.raises(QualityGateFailure):
        gate.enforce(coll)

def test_min_row_count_pass():
    """EPIC-006-F-011-S-001-REQ-T-002: must pass when count >= min_rows"""
    coll = MockCollection([{"npi": str(i)} for i in range(100)])
    gate = QualityGate("test", min_rows=100)
    result = gate.enforce(coll)
    assert result["row_count"]["passed"] is True

def test_null_check_fail():
    """EPIC-006-F-011-S-001-REQ-T-003: detect nulls exceeding threshold"""
    docs = [{"npi": str(i), "state": None} for i in range(100)]
    coll = MockCollection(docs)
    gate = QualityGate("test", min_rows=1, required_fields=["state"], max_null_fraction=0.05)
    with pytest.raises(QualityGateFailure):
        gate.enforce(coll)

def test_null_check_pass():
    """EPIC-006-F-011-S-001-REQ-T-004: pass when nulls within threshold"""
    docs = [{"npi": str(i), "state": "DE"} for i in range(100)]
    coll = MockCollection(docs)
    gate = QualityGate("test", min_rows=1, required_fields=["state"], max_null_fraction=0.05)
    result = gate.enforce(coll)
    assert result["null_checks"]["passed"] is True
