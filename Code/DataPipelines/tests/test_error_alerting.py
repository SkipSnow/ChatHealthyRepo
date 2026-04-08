# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""FatalAlertBridge tests — PIPE-FA-001"""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from fatal_alert_bridge import FatalAlertBridge

def test_alert_on_fatal_error():
    """PIPE-FA-001-REQ-001: send_alert must not crash"""
    bridge = FatalAlertBridge(db_client=None)
    # Should not raise on any error type
    bridge.send_alert(RuntimeError("test error"), context="unit_test")
    bridge.send_alert(ValueError("bad value"), context="unit_test")
    bridge.send_alert(Exception("generic"), context="unit_test")

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
    """PIPE-FA-001-REQ-003: stop_bell terminates loop"""
    FatalAlertBridge.stop_bell()  # Should not crash even if bell never started
