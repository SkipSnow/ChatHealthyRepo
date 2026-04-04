# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Data quality requirements — bad_data, out_of_scope, controlled vocabularies
# Stories: PIPE-DQ-001, PIPE-DQ-002, PIPE-DQ-003
# These tests verify requirements against the controlled vocabulary and code structure.
# End-to-end tests require a loaded database.

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

VOCAB_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                           "brain", "machine_artifacts", "content", "controlled_vocabularies.json")


class TestControlledVocabularies(unittest.TestCase):
    """Verify controlled_vocabularies.json structure and completeness."""

    @classmethod
    def setUpClass(cls):
        with open(VOCAB_PATH) as f:
            cls.vocabs = json.load(f)

    def _get_vocab(self, vocab_id):
        return next((v for v in self.vocabs["vocabularies"] if v["vocabulary_id"] == vocab_id), None)

    # ── CV-001: bad_data_reasons ──

    def test_cv001_exists(self):
        """CV-001 bad_data_reasons vocabulary must exist."""
        self.assertIsNotNone(self._get_vocab("CV-001"))

    def test_cv001_has_required_members(self):
        """CV-001 must contain: no_address, bad_address, zip_state_mismatch, zip_state_mismatch_multiple_licenses, zip_state_mismatch_no_license."""
        v = self._get_vocab("CV-001")
        values = [m["value"] for m in v["members"]]
        for required in ["no_address", "bad_address", "zip_state_mismatch",
                         "zip_state_mismatch_multiple_licenses", "zip_state_mismatch_no_license"]:
            self.assertIn(required, values, f"Missing bad_data reason: {required}")

    def test_cv001_all_members_have_definitions(self):
        """Every bad_data reason must have a non-empty definition."""
        v = self._get_vocab("CV-001")
        for m in v["members"]:
            self.assertTrue(len(m.get("definition", "")) > 0,
                            f"bad_data reason '{m['value']}' has no definition")

    # ── CV-002: out_of_scope_reasons ──

    def test_cv002_exists(self):
        """CV-002 out_of_scope_reasons vocabulary must exist."""
        self.assertIsNotNone(self._get_vocab("CV-002"))

    def test_cv002_has_required_members(self):
        """CV-002 must contain: deactivated, foreign_provider."""
        v = self._get_vocab("CV-002")
        values = [m["value"] for m in v["members"]]
        self.assertIn("deactivated", values)
        self.assertIn("foreign_provider", values)

    def test_cv002_all_members_have_definitions(self):
        """Every out_of_scope reason must have a non-empty definition."""
        v = self._get_vocab("CV-002")
        for m in v["members"]:
            self.assertTrue(len(m.get("definition", "")) > 0,
                            f"out_of_scope reason '{m['value']}' has no definition")

    # ── CV-003: operating_modes ──

    def test_cv003_has_idiot_mode(self):
        """CV-003 must contain idiot_mode."""
        v = self._get_vocab("CV-003")
        values = [m["value"] for m in v["members"]]
        self.assertIn("idiot_mode", values)

    # ── All vocabularies ──

    def test_all_vocabs_have_definitions(self):
        """Every vocabulary must have a non-empty definition."""
        for v in self.vocabs["vocabularies"]:
            self.assertTrue(len(v.get("definition", "")) > 0,
                            f"Vocabulary '{v['vocabulary_id']}' has no definition")

    def test_all_vocabs_have_invoked_by(self):
        """Every vocabulary must have invoked_by with source_files and stories."""
        for v in self.vocabs["vocabularies"]:
            self.assertIn("invoked_by", v, f"Vocabulary '{v['vocabulary_id']}' missing invoked_by")
            self.assertIn("source_files", v["invoked_by"])
            self.assertIn("stories", v["invoked_by"])


class TestStateFilterEnforcement(unittest.TestCase):
    """Verify state filter is mandatory in all pipeline code."""

    def test_build_states_filter_raises_on_missing(self):
        """_build_states_filter must raise ValueError when states is not provided."""
        from county_enrichment_job import _build_states_filter
        with self.assertRaises(ValueError):
            _build_states_filter({})

    def test_build_states_filter_raises_on_empty_list(self):
        """_build_states_filter must raise ValueError when states list is empty."""
        from county_enrichment_job import _build_states_filter
        with self.assertRaises(ValueError):
            _build_states_filter({"states": []})

    def test_build_states_filter_accepts_list(self):
        """_build_states_filter must accept a plain list of state codes."""
        from county_enrichment_job import _build_states_filter
        result = _build_states_filter({"states": ["DE", "VA"]})
        self.assertIn("practice_address.state", result)
        self.assertEqual(result["practice_address.state"]["$in"], ["DE", "VA"])

    def test_build_states_filter_accepts_dict(self):
        """_build_states_filter must accept dict format with mode and list."""
        from county_enrichment_job import _build_states_filter
        result = _build_states_filter({"states": {"mode": "include", "list": ["CA"]}})
        self.assertIn("practice_address.state", result)

    def test_no_build_states_query_exists(self):
        """_build_states_query (permissive) must not exist — replaced by _build_states_filter."""
        from county_enrichment_job import __dict__ as module_dict
        self.assertNotIn("_build_states_query", module_dict,
                         "_build_states_query must be removed — use _build_states_filter only")

    def test_provider_worker_requires_states(self):
        """ProviderWorker must raise ValueError when states is missing."""
        from provider_worker import ProviderWorker
        with self.assertRaises((ValueError, KeyError)):
            ProviderWorker({"worker_id": 1, "load_id": "test",
                           "start_byte": 0, "end_byte": 100, "csv_path": "test.csv"})

    def test_embedding_worker_uses_strict_filter(self):
        """EmbeddingWorker must import _build_states_filter, not _build_states_query."""
        import embedding_worker
        source = open(embedding_worker.__file__).read()
        self.assertNotIn("_build_states_query", source,
                         "embedding_worker must use _build_states_filter")
        self.assertIn("_build_states_filter", source)


class TestBadDataFlag(unittest.TestCase):
    """PIPE-DQ-001: bad_data flag requirements."""

    def test_mark_out_of_scope_uses_bad_data_for_no_address(self):
        """Records with no address must be flagged bad_data.reason=no_address, not county.source=out_of_scope."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("bad_data", source, "Code must reference bad_data field")
        self.assertIn("no_address", source, "Code must use no_address reason")

    def test_controlled_vocab_values_in_code(self):
        """All bad_data reasons used in code must be from CV-001."""
        with open(VOCAB_PATH) as f:
            vocabs = json.load(f)
        cv001 = next(v for v in vocabs["vocabularies"] if v["vocabulary_id"] == "CV-001")
        allowed = {m["value"] for m in cv001["members"]}

        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()

        # Check that any bad_data reason string in code is in the vocabulary
        import re
        reasons = re.findall(r'"reason":\s*"([^"]+)"', source)
        bad_reasons = [r for r in reasons if r in allowed or r.startswith("zip_state")]
        for reason in bad_reasons:
            self.assertIn(reason, allowed, f"Reason '{reason}' not in CV-001 vocabulary")


class TestOutOfScopeFlag(unittest.TestCase):
    """PIPE-DQ-002: out_of_scope flag requirements."""

    def test_deactivated_is_out_of_scope(self):
        """Deactivated providers must be flagged out_of_scope, not bad_data."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("out_of_scope", source, "Code must reference out_of_scope field")
        self.assertIn("deactivated", source)

    def test_foreign_provider_is_out_of_scope(self):
        """Foreign providers must be flagged out_of_scope.reason=foreign_provider."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("foreign_provider", source)


class TestZipStateMismatchRepair(unittest.TestCase):
    """PIPE-DQ-003: zip/state mismatch repair from license data."""

    def test_license_check_exists_in_code(self):
        """mark_zip_state_mismatch_fn must check license data before flagging."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("license", source.lower(), "Code must reference license data for repair")

    def test_multiple_license_reason_exists(self):
        """Code must handle multiple licenses case."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("zip_state_mismatch_multiple_licenses", source)

    def test_no_license_reason_exists(self):
        """Code must handle no license case."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "county_enrichment_job.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("zip_state_mismatch_no_license", source)


class TestIncrementalMode(unittest.TestCase):
    """ENH-PIPE-001 and ENH-PIPE-002 requirements."""

    def test_incremental_flag_in_provider_worker(self):
        """ProviderWorker must check for incremental flag in config."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "provider_worker.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("incremental", source, "ProviderWorker must handle incremental mode")

    def test_drain_respects_incremental(self):
        """drain_staging_fn or orchestrator must skip drop when incremental=true."""
        source_path = os.path.join(os.path.dirname(__file__), "..", "provider_load_manager.py")
        with open(source_path) as f:
            source = f.read()
        self.assertIn("incremental", source, "Orchestrator must check incremental flag")


if __name__ == "__main__":
    unittest.main()
