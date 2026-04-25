# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""SchemaDriftDetector tests — PIPE-SD-001"""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from schema_drift_detector import SchemaDriftDetector, SchemaDriftError

class MockCollection:
    """Minimal mock that returns docs from find().limit()"""
    def __init__(self, docs):
        self._docs = docs
    def find(self, *args, **kwargs):
        return MockCursor(self._docs)

class MockCursor:
    def __init__(self, docs):
        self._docs = docs
    def limit(self, n):
        return self._docs[:n]

class MockFPCollection:
    """Mock for admin.SchemaFingerprints"""
    def __init__(self):
        self._store = {}
    def find_one(self, query):
        name = query.get("collection_name")
        return self._store.get(name)
    def update_one(self, filter, update, upsert=False):
        name = filter.get("collection_name")
        self._store[name] = update.get("$set", {})

class MockClient:
    def __init__(self):
        self.fp_coll = MockFPCollection()
    def __getitem__(self, db_name):
        return {"SchemaFingerprints": self.fp_coll}

def test_drift_detected():
    """EPIC-006-F-013-S-001-REQ-T-001: raises SchemaDriftError on schema change"""
    detector = SchemaDriftDetector()
    client = MockClient()
    # Store fingerprint for schema A
    coll_a = MockCollection([{"name": "John", "age": 30}])
    detector.store_fingerprint(coll_a, "test_coll", client)
    # Check against schema B (different fields)
    coll_b = MockCollection([{"title": "Dr", "score": 99.5}])
    with pytest.raises(SchemaDriftError):
        detector.detect_and_alert(coll_b, "test_coll", client)

def test_no_drift():
    """EPIC-006-F-013-S-001-REQ-T-002: passes when schema unchanged"""
    detector = SchemaDriftDetector()
    client = MockClient()
    coll = MockCollection([{"name": "John", "age": 30}])
    detector.store_fingerprint(coll, "test_coll", client)
    result = detector.detect_and_alert(coll, "test_coll", client)
    assert result["passed"] is True

def test_store_fingerprint():
    """EPIC-006-F-013-S-001-REQ-T-003: fingerprint stored in admin.SchemaFingerprints"""
    detector = SchemaDriftDetector()
    client = MockClient()
    coll = MockCollection([{"npi": "123", "state": "DE"}])
    fp = detector.store_fingerprint(coll, "providers", client)
    assert isinstance(fp, str)
    assert len(fp) == 64  # SHA-256 hex
    stored = client.fp_coll._store.get("providers")
    assert stored is not None
    assert stored["fingerprint"] == fp

def test_allow_drift_override():
    """EPIC-006-F-013-S-001-REQ-T-004: allow_drift=True suppresses error"""
    detector = SchemaDriftDetector()
    client = MockClient()
    coll_a = MockCollection([{"name": "John"}])
    detector.store_fingerprint(coll_a, "test_coll", client)
    coll_b = MockCollection([{"totally": "different"}])
    # Should NOT raise
    result = detector.detect_and_alert(coll_b, "test_coll", client, allow_drift=True)
    assert result["passed"] is False  # drift detected but not raised
