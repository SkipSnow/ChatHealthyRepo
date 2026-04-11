# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""FatalAlertBridge tests — PIPE-FA-001"""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fatal_alert_bridge import FatalAlertBridge

def test_alert_on_fatal_error():
    """PIPE-FA-001-REQ-001: send_alert must not crash on any error type"""
    bridge = FatalAlertBridge(db_client=None)
    # Must not raise on any error type — if it does, pytest catches the exception
    for err in [RuntimeError("test"), ValueError("bad"), Exception("generic"), OSError("io")]:
        bridge.send_alert(err, context="unit_test")
    assert True  # reached here without raising

def test_alert_logs_to_mongodb():
    """PIPE-FA-001-REQ-002: event written to admin.BellEvents"""
    # Mock a minimal MongoDB client
    class MockColl:
        def __init__(self):
            self.docs = []
        def insert_one(self, doc):
            self.docs.append(doc)
    class MockDB:
        def __init__(self):
            self.bell = MockColl()
        def __getitem__(self, name):
            return self.bell
    class MockClient:
        def __init__(self):
            self.db = MockDB()
        def __getitem__(self, name):
            return self.db

    client = MockClient()
    bridge = FatalAlertBridge(db_client=client)
    bridge.send_alert(RuntimeError("test"), context="unit_test")
    assert len(client.db.bell.docs) == 1
    assert client.db.bell.docs[0]["error_type"] == "RuntimeError"

def test_bell_stop():
    """PIPE-FA-001-REQ-003: stop_bell terminates loop without error"""
    FatalAlertBridge.stop_bell()
    assert True  # reached here without raising
