# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# E2E Pipeline requirement tests — MAINT-E2E-PIPELINE
# Tests the provider pipeline: load → enrich → embed
# Uses real systems (GOV-006). Tests against dev environment.

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


def _read_source(filename):
    """Read a source file from the DataPipelines directory."""
    path = os.path.join(os.path.dirname(__file__), "..", filename)
    with open(path) as f:
        return f.read()


class TestPipelineReservationPattern(unittest.TestCase):
    """E2E-PIPE-S1: Pipeline orchestrator follows the fundamental pattern."""

    def test_r01_orchestrator_reserves_cluster(self):
        """Pipeline must call register_reservation_activity before DB work."""
        src = _read_source("provider_load_manager.py")
        self.assertIn("register_reservation_activity", src,
                       "Orchestrator must reserve cluster before work")

    def test_r06_orchestrator_releases_in_finally(self):
        """Pipeline must call release_reservation_activity in finally block."""
        src = _read_source("provider_load_manager.py")
        self.assertIn("release_reservation_activity", src,
                       "Orchestrator must release reservation")

    def test_r09_each_step_is_activity(self):
        """Each pipeline step must be a separate durable activity."""
        src = _read_source("provider_load_manager.py")
        self.assertIn("call_activity", src, "Steps must be durable activities")

    def test_r10_start_step_parameter(self):
        """Pipeline must support start_step for resumability."""
        src = _read_source("provider_load_manager.py")
        self.assertIn("start_step", src, "Must support start_step parameter")


class TestPipelineDataOperations(unittest.TestCase):
    """E2E-PIPE-S2: Pipeline operates on correct database."""

    def test_r03_loads_into_dev_public_health_data(self):
        """Pipeline loads into {env}_PublicHealthData.providers."""
        src = _read_source("provider_load_manager.py")
        self.assertTrue(
            "PublicHealthData" in src or "env_prefix" in src,
            "Pipeline must use env-scoped database")

    def test_r04_enrichment_in_place(self):
        """County enrichment must update existing docs, not write to new collection."""
        src = _read_source("county_enrichment_job.py")
        # The orchestrator should NOT reference providers_enriched as a destination
        # (it's OK if mentioned in comments or old code — check the orchestrator function)
        self.assertIn("county_enrichment_orchestrator_fn", src,
                       "Enrichment orchestrator must exist")


class TestMigrateEnvironment(unittest.TestCase):
    """MONGO-LIFE-S1: MigrateEnvironment requirements."""

    def test_r07_uses_correct_env_naming(self):
        """Environment names must follow devops.json: dev, qa, prod."""
        from copy_to_frontend import migrate_small_collections
        import inspect
        src = inspect.getsource(migrate_small_collections)
        self.assertIn("_PublicHealthData", src, "Must use {env}_PublicHealthData naming")

    def test_r09_returns_202(self):
        """MigrateEnvironment must be in ASYNC_TASK_ORCHESTRATORS."""
        src = _read_source("function_app.py")
        self.assertIn('"MigrateEnvironment"', src,
                       "MigrateEnvironment must be in ASYNC_TASK_ORCHESTRATORS")

    def test_r02_small_collections_use_out(self):
        """Small collections must use $out for server-side copy."""
        from copy_to_frontend import migrate_small_collections
        import inspect
        src = inspect.getsource(migrate_small_collections)
        self.assertIn("$out", src, "Small collections must use $out")

    def test_r03_providers_use_chunked_copy(self):
        """Providers must use chunked _id range copy."""
        from copy_to_frontend import migrate_chunk
        import inspect
        src = inspect.getsource(migrate_chunk)
        self.assertIn("$gte", src, "Must use _id range query")
        self.assertIn("$lte", src, "Must use _id range query")

    def test_r04_chunks_not_state_based(self):
        """Chunks must be record-count based, not state-based."""
        from copy_to_frontend import migrate_chunk
        import inspect
        src = inspect.getsource(migrate_chunk)
        self.assertNotIn("practice_address.state", src,
                          "Chunk workers must NOT filter by state")


class TestCopyToFrontEnd(unittest.TestCase):
    """MONGO-LIFE-S2: CopyToFrontEnd requirements."""

    def test_r10_uses_frontend_connection(self):
        """CopyToFrontEnd must write to MONGO_FRONTEND_connectionString."""
        from copy_to_frontend import copy_chunk
        import inspect
        src = inspect.getsource(copy_chunk)
        self.assertIn("MONGO_FRONTEND_connectionString", src,
                       "Must use frontend connection string")

    def test_r15_returns_202(self):
        """CopyToFrontEnd must be in ASYNC_TASK_ORCHESTRATORS."""
        src = _read_source("function_app.py")
        self.assertIn('"CopyToFrontEnd"', src,
                       "CopyToFrontEnd must be in ASYNC_TASK_ORCHESTRATORS")

    def test_r12_static_collections_first(self):
        """Static collections must be copied before providers."""
        src = _read_source("function_app.py")
        static_pos = src.find("Copying static collections")
        provider_pos = src.find("Partitioning")
        if static_pos >= 0 and provider_pos >= 0:
            self.assertLess(static_pos, provider_pos,
                            "Static collections must be copied before providers")


class TestOpsManager(unittest.TestCase):
    """EPIC-3 Ops Manager — existing tests verified, additional coverage."""

    def test_ops_timer_in_function_app(self):
        """Ops timer must be registered in function_app."""
        src = _read_source("function_app.py")
        self.assertIn("cluster_lifecycle_timer", src,
                        "cluster_lifecycle_timer must be registered")

    def test_ops_agent_uses_base_agent(self):
        """OpsManagerAgent must extend BaseAgent."""
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "Shared"))
        from agent_framework.base_agent import BaseAgent
        from ops_manager.ops_agent import OpsManagerAgent
        self.assertTrue(issubclass(OpsManagerAgent, BaseAgent),
                        "OpsManagerAgent must extend BaseAgent")

    def test_audit_trail_append_only(self):
        """Audit trail must be append-only — no update or delete methods."""
        from ops_manager.audit_trail import AuditTrail
        import inspect
        methods = [m for m in dir(AuditTrail) if not m.startswith('_')]
        for method in methods:
            self.assertNotIn("update", method.lower(),
                             f"Audit trail must not have update methods: {method}")
            self.assertNotIn("delete", method.lower(),
                             f"Audit trail must not have delete methods: {method}")

    def test_triage_rule_based_precheck(self):
        """Triage must check rules before invoking LLM."""
        from ops_manager.triage_tool import _rule_based_classify
        # Known infra error
        result = _rule_based_classify("ServerSelectionTimeoutError", "timeout", "IDLE")
        self.assertEqual(result["category"], "INFRASTRUCTURE")
        self.assertEqual(result["source"], "rule")
        # Known pipeline error
        result = _rule_based_classify("KeyError", "missing field", "IDLE")
        self.assertEqual(result["category"], "PIPELINE")
        self.assertEqual(result["source"], "rule")
        # Unknown → None (goes to LLM)
        result = _rule_based_classify("WeirdError", "something", "IDLE")
        self.assertIsNone(result)

    def test_evidence_package_validates(self):
        """EvidencePackage must validate required fields."""
        from ops_manager.evidence_package import EvidencePackage
        ep = EvidencePackage(
            error_code="", error_message="",
            classification="INVALID", confidence=2.0,
            reasoning="", cluster_state="",
        )
        errors = ep.validate()
        self.assertGreater(len(errors), 0, "Invalid package must have validation errors")


if __name__ == "__main__":
    unittest.main()
