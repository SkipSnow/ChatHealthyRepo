# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Unit tests for OpsManagerAgent — happy path + error paths.
# Uses mocked lifecycle manager (no real Atlas or MongoDB needed).

import sys
import os
import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ops_manager.ops_agent import OpsManagerAgent
from ops_manager.evidence_package import EvidencePackage
from ops_manager.triage_tool import _rule_based_classify


class MockLifecycleManager:
    """Mock cluster lifecycle manager for unit tests."""

    def __init__(self):
        self._state = {"_id": "cluster_reservations", "reservations": []}
        self._cluster_state = "IDLE"
        self.wake_calls = []
        self.pause_calls = []

    def reserve(self, cluster_name, job_id, requester, expected_duration_minutes, expected_min_minutes=0):
        now = datetime.now(timezone.utc)
        reservation = {
            "job_id": job_id,
            "requester": requester,
            "cluster_name": cluster_name,
            "expected_duration_minutes": expected_duration_minutes,
            "expected_min_minutes": expected_min_minutes,
            "start_time": now.isoformat(),
            "expected_end_time": (now + timedelta(minutes=expected_duration_minutes)).isoformat(),
            "status": "active",
        }
        self._state["reservations"].append(reservation)
        self.wake_calls.append(cluster_name)
        return reservation

    def status(self, cluster_name):
        active = [r for r in self._state["reservations"] if r.get("cluster_name") == cluster_name]
        return {
            "cluster_name": cluster_name,
            "cluster_state": self._cluster_state,
            "active_reservations": len(active),
            "reservations": active,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

    def release(self, job_id):
        before = len(self._state["reservations"])
        self._state["reservations"] = [r for r in self._state["reservations"] if r.get("job_id") != job_id]
        after = len(self._state["reservations"])
        if after == 0:
            self.pause_calls.append("ChatHealthyDataPipelines")
        return {"released": job_id, "remaining": after}

    def force_release_all(self, cluster_name):
        count = len(self._state["reservations"])
        self._state["reservations"] = []
        self.pause_calls.append(cluster_name)
        return {"released": count, "cluster": cluster_name}

    def _send_wake(self, cluster_name):
        self.wake_calls.append(cluster_name)

    def _send_pause(self, cluster_name):
        self.pause_calls.append(cluster_name)


class TestHappyPath(unittest.TestCase):
    """V1 success criteria: wake -> copy -> release -> pause."""

    def setUp(self):
        self.mgr = MockLifecycleManager()
        self.notifications = []
        self.agent = OpsManagerAgent(
            lifecycle_manager=self.mgr,
            get_db_fn=lambda: None,  # no real DB for unit tests
            env_prefix="test",
            push_fn=lambda t, m: self.notifications.append(("push", t, m)),
            email_fn=lambda s, t: self.notifications.append(("email", s, t)),
        )

    def test_wake_reserve_release_pause(self):
        """Happy path: wake cluster, do work, release, cluster pauses."""
        # Wake
        result = self.agent.handle_event({
            "type": "wake",
            "params": {
                "cluster_name": "ChatHealthyDataPipelines",
                "job_id": "test_copy_001",
                "requester": "CopyToFrontEnd",
                "expected_duration_minutes": 120,
            },
        })
        self.assertTrue(result.success)
        self.assertEqual(result.data["job_id"], "test_copy_001")
        self.assertEqual(len(self.mgr.wake_calls), 1)

        # Status check
        result = self.agent.handle_event({
            "type": "status",
            "params": {"cluster_name": "ChatHealthyDataPipelines"},
        })
        self.assertTrue(result.success)
        self.assertEqual(result.data["cluster_state"], "IDLE")
        self.assertEqual(result.data["active_reservations"], 1)

        # Release
        result = self.agent.handle_event({
            "type": "release",
            "params": {"job_id": "test_copy_001"},
        })
        self.assertTrue(result.success)
        self.assertEqual(result.data["remaining"], 0)

        # Cluster should have been paused (last reservation released)
        self.assertEqual(len(self.mgr.pause_calls), 1)

    def test_timer_with_no_reservations(self):
        """Timer on idle cluster with no reservations — no alerts."""
        result = self.agent.handle_event({
            "type": "timer_check",
            "cluster_name": "ChatHealthyDataPipelines",
        })
        self.assertTrue(result.success)


class TestRuleBasedTriage(unittest.TestCase):
    """Test the rule-based precheck classifier."""

    def test_connection_failure_is_infra(self):
        result = _rule_based_classify("ServerSelectionTimeoutError", "timeout", "IDLE")
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "INFRASTRUCTURE")
        self.assertEqual(result["source"], "rule")

    def test_key_error_is_pipeline(self):
        result = _rule_based_classify("KeyError", "missing 'provider_npi'", "IDLE")
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "PIPELINE")

    def test_paused_cluster_is_infra(self):
        result = _rule_based_classify("", "", "PAUSED")
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "INFRASTRUCTURE")
        self.assertTrue(result["self_repair_possible"])

    def test_unknown_error_returns_none(self):
        result = _rule_based_classify("WeirdCustomError", "something unexpected", "IDLE")
        self.assertIsNone(result)

    def test_validation_error_is_pipeline(self):
        result = _rule_based_classify("ValidationError", "field 'npi' required", "IDLE")
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "PIPELINE")

    def test_auth_failure_is_infra(self):
        result = _rule_based_classify("AuthenticationFailed", "bad credentials", "IDLE")
        self.assertIsNotNone(result)
        self.assertEqual(result["category"], "INFRASTRUCTURE")


class TestEvidencePackage(unittest.TestCase):
    """Test EvidencePackage validation and capping."""

    def test_valid_package(self):
        ep = EvidencePackage(
            error_code="KeyError", error_message="missing npi",
            classification="PIPELINE", confidence=0.9,
            reasoning="KeyError in transform", cluster_state="IDLE",
        )
        self.assertEqual(ep.validate(), [])

    def test_invalid_classification(self):
        ep = EvidencePackage(
            error_code="Err", error_message="msg",
            classification="INVALID", confidence=0.5,
            reasoning="test", cluster_state="IDLE",
        )
        errors = ep.validate()
        self.assertTrue(any("classification" in e for e in errors))

    def test_recent_events_capped(self):
        events = [{"msg": f"event_{i}"} for i in range(20)]
        ep = EvidencePackage(
            error_code="Err", error_message="msg",
            classification="PIPELINE", confidence=0.5,
            reasoning="test", cluster_state="IDLE",
            recent_events=events,
        )
        self.assertEqual(len(ep.recent_events), 10)

    def test_confidence_out_of_range(self):
        ep = EvidencePackage(
            error_code="Err", error_message="msg",
            classification="PIPELINE", confidence=1.5,
            reasoning="test", cluster_state="IDLE",
        )
        errors = ep.validate()
        self.assertTrue(any("confidence" in e for e in errors))


class TestErrorFlow(unittest.TestCase):
    """Test error handling: triage -> route -> repair/escalate."""

    def setUp(self):
        self.mgr = MockLifecycleManager()
        self.notifications = []
        self.agent = OpsManagerAgent(
            lifecycle_manager=self.mgr,
            get_db_fn=lambda: None,
            env_prefix="test",
            push_fn=lambda t, m: self.notifications.append(("push", t, m)),
            email_fn=lambda s, t: self.notifications.append(("email", s, t)),
        )

    def test_infra_error_triggers_repair(self):
        """Infrastructure error → repair loop → success (cluster becomes IDLE)."""
        result = self.agent.handle_event({
            "type": "error",
            "error_code": "ServerSelectionTimeoutError",
            "error_message": "Could not connect to MongoDB",
            "cluster_name": "ChatHealthyDataPipelines",
            "job_id": "test_job_001",
            "requester": "CopyToFrontEnd",
        })
        # Since mock always returns IDLE, repair should succeed
        self.assertTrue(result.success)
        self.assertTrue(result.data.get("repaired"))
        # Should have notification for error detected + self-repaired
        push_titles = [n[1] for n in self.notifications if n[0] == "push"]
        self.assertTrue(any("Error Detected" in t for t in push_titles))
        self.assertTrue(any("Self-Repaired" in t for t in push_titles))

    def test_pipeline_error_routes_to_dev(self):
        """Pipeline error → evidence package → Dev Manager."""
        result = self.agent.handle_event({
            "type": "error",
            "error_code": "KeyError",
            "error_message": "KeyError: 'provider_npi' in transform_providers.py",
            "cluster_name": "ChatHealthyDataPipelines",
            "job_id": "test_job_002",
            "requester": "CountyEnrichment",
        })
        self.assertTrue(result.success)
        self.assertEqual(result.data.get("routed_to"), "dev_manager")
        self.assertIn("evidence_package", result.data)
        ep = result.data["evidence_package"]
        self.assertEqual(ep["classification"], "PIPELINE")


if __name__ == "__main__":
    unittest.main()
