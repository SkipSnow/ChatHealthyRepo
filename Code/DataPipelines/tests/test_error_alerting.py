# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""BellRinger tests — EPIC-008-F-004-S-009 (Notify Human user with work
station generated sound)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), "..", "..", "..",
    "architecture", "DevOpsBuildDeployAndEnvironmentManagement",
))
from bell_ringer import BellRinger


def test_log_event_and_ring_does_not_crash_on_any_priority():
    """Class must accept normal/high/fatal priorities without raising."""
    ringer = BellRinger(db_client=None)
    for priority in ("normal", "high", "fatal"):
        ringer.log_event_and_ring(
            event_type="UnitTest",
            message=f"test {priority}",
            context="unit_test",
            priority=priority,
        )
    ringer.stop()


def test_log_event_writes_to_admin_bell_events():
    """Configured db_client receives the event document in admin.BellEvents."""
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
    ringer = BellRinger(db_client=client)
    ringer.log_event_and_ring(
        event_type="RuntimeError",
        message="test",
        context="unit_test",
        priority="normal",
    )
    assert len(client.db.bell.docs) == 1
    assert client.db.bell.docs[0]["event_type"] == "RuntimeError"
    assert client.db.bell.docs[0]["priority"] == "normal"


def test_stop_terminates_continuous_loop_without_error():
    """stop() returns cleanly even with no active loop, and after starting one."""
    ringer = BellRinger()
    ringer.stop()  # idempotent: no active loop
    ringer.ring_until_acknowledged(interval_seconds=0.1)
    ringer.stop()


def test_ring_n_times_with_zero_or_negative_is_noop():
    """Defensive: ring_n_times(0) and ring_n_times(-3) do not raise."""
    ringer = BellRinger()
    ringer.ring_n_times(0)
    ringer.ring_n_times(-3)
