# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""
Tests for compliance_crosswalk_agent.py (single-class refactor).

Pass 1 deterministic logic is exercised with synthetic OSCAL fixtures and
synthetic security.json fixtures. Pass 2 (LLM) is exercised with the
Anthropic client patched so the suite never hits the network.

Public surface under test:
- ComplianceCrosswalkAgent.__init__
- ComplianceCrosswalkAgent.ComplianceCrossWalk()  (Pass 1 + Pass 2)
- ComplianceCrosswalkAgent.ComplianceCrosswalkReport()
- ComplianceCrosswalkAgent.run()
- FileType enum
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def agent_mod():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    mod = importlib.import_module("compliance_crosswalk_agent")
    importlib.reload(mod)
    return mod


def _fake_oscal(controls_payload):
    """Build a minimal OSCAL JSON dict with the given controls under one group."""
    return {"catalog": {"groups": [{"id": "sc",
                                    "title": "System and Communications Protection",
                                    "controls": controls_payload}]}}


def _patch_oscal_response(agent_mod, payload):
    return patch.object(agent_mod.urllib.request, "urlopen",
                        new=_make_urlopen_mock(payload))


def _make_urlopen_mock(payload):
    def _urlopen(req, timeout=None):
        cm = MagicMock()
        cm.read.return_value = json.dumps(payload).encode("utf-8")
        ctx = MagicMock()
        ctx.__enter__ = lambda self: cm
        ctx.__exit__ = lambda self, exc_type, exc, tb: False
        return ctx
    return _urlopen


# ── FileType enum ────────────────────────────────────────────────


class TestFileType:
    def test_all_returns_full_extension_set(self, agent_mod):
        exts = agent_mod.FileType.ALL.extensions()
        for needed in (".py", ".json", ".html", ".yml", ".yaml", ".sh"):
            assert needed in exts

    def test_python_returns_py_only(self, agent_mod):
        assert agent_mod.FileType.PYTHON.extensions() == {".py"}

    def test_yaml_returns_both_yml_and_yaml(self, agent_mod):
        assert agent_mod.FileType.YAML.extensions() == {".yml", ".yaml"}

    def test_value_round_trips_through_constructor(self, agent_mod):
        assert agent_mod.FileType("all") is agent_mod.FileType.ALL
        assert agent_mod.FileType("py") is agent_mod.FileType.PYTHON


# ── Pass 1: NIST OSCAL ingestion ─────────────────────────────────


class TestNistOscalIngestion:
    def test_extracts_flat_controls(self, agent_mod, tmp_path):
        oscal = _fake_oscal([
            {"id": "sc-8", "title": "Transmission Confidentiality",
             "parts": [{"name": "statement",
                        "prose": "Protect information in transit."}]},
        ])
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json",
            api_key="sk-fake-not-used")
        with _patch_oscal_response(agent_mod, oscal):
            controls = agent._load_nist_oscal()
        assert len(controls) == 1
        c = controls[0]
        assert c.id == "SC-8"
        assert c.family == "SC System and Communications Protection"
        assert c.title == "Transmission Confidentiality"
        assert "Protect information in transit" in c.control_text

    def test_extracts_nested_enhancements(self, agent_mod, tmp_path):
        oscal = _fake_oscal([{
            "id": "sc-8", "title": "Parent",
            "parts": [{"name": "statement", "prose": "parent text"}],
            "controls": [{"id": "sc-8.1", "title": "Enhancement",
                          "parts": [{"name": "statement",
                                     "prose": "child text"}]}],
        }])
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json",
            api_key="sk-fake")
        with _patch_oscal_response(agent_mod, oscal):
            controls = agent._load_nist_oscal()
        ids = {c.id for c in controls}
        assert ids == {"SC-8", "SC-8.1"}

    def test_part_to_text_recurses_into_subparts(self, agent_mod, tmp_path):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        part = {"name": "statement", "prose": "top",
                "parts": [{"name": "item", "prose": "child a"},
                          {"name": "item", "prose": "child b",
                           "parts": [{"name": "sub", "prose": "grandchild"}]}]}
        text = agent._part_to_text(part)
        for token in ("top", "child a", "child b", "grandchild"):
            assert token in text


# ── Pass 1: HITRUST seed from security.json ──────────────────────


class TestHitrustSeed:
    def test_returns_empty_when_security_json_missing(self, agent_mod, tmp_path):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "nope.json", api_key="sk-fake")
        assert agent._load_hitrust_seed() == []

    def test_extracts_controls_from_security_json(self, agent_mod, tmp_path):
        sec = {"records": [{"compliance_mapping": {"hitrust_csf": {
            "domains": [{"domain": "09 Communications and Operations",
                         "controls_satisfied": [
                             {"id": "09.s",
                              "control": "Information Exchange Policies",
                              "how": "mTLS for all service-to-service"}]}]}}}]}
        sec_path = tmp_path / "security.json"
        sec_path.write_text(json.dumps(sec), encoding="utf-8")
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=sec_path, api_key="sk-fake")
        controls = agent._load_hitrust_seed()
        assert len(controls) == 1
        assert controls[0].id == "09.s"
        assert "mTLS" in controls[0].how_seed


# ── Pass 1: crosswalk assembly ───────────────────────────────────


class TestCrosswalkBuild:
    def test_nist_only_control_emits_no_hitrust_block(self, agent_mod, tmp_path):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        nist = [agent_mod._NistControl(id="ZZ-99", family="ZZ",
                                       title="Zed", control_text="x")]
        doc = agent._build_crosswalk(nist, [])
        assert doc["control_count"] == 1
        c = doc["controls"][0]
        assert c["frameworks"] == ["nist_800_53"]
        assert c["hitrust_csf"] is None
        assert c["implementations"] == []

    def test_seed_crosswalk_attaches_hitrust_block(self, agent_mod, tmp_path):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        nist = [agent_mod._NistControl(id="SC-8", family="SC",
                                       title="Transmission",
                                       control_text="protect")]
        hitrust = [agent_mod._HitrustControl(id="09.s",
                                             domain="09 Comms",
                                             control="Info Exchange",
                                             how_seed="...")]
        doc = agent._build_crosswalk(nist, hitrust)
        c = doc["controls"][0]
        assert "hitrust_csf" in c["frameworks"]
        assert "09.s" in c["hitrust_csf"]["ids"]
        assert any(d["id"] == "09.s" for d in c["hitrust_csf"]["details"])

    def test_hitrust_only_control_emits_standalone_entry(self, agent_mod, tmp_path):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        hitrust = [agent_mod._HitrustControl(id="13.zz",
                                             domain="13 Test Domain",
                                             control="Something")]
        doc = agent._build_crosswalk([], hitrust)
        assert doc["control_count"] == 1
        c = doc["controls"][0]
        assert c["control_id"] == "HITRUST:13.zz"
        assert c["frameworks"] == ["hitrust_csf"]
        assert c["nist_800_53"] is None

    def test_top_level_metadata_present(self, agent_mod, tmp_path):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        doc = agent._build_crosswalk([], [])
        for key in ("$schema", "collection", "version", "date",
                    "frameworks", "control_count", "controls"):
            assert key in doc
        assert doc["collection"] == "SecurityAuditControls"


# ── File walker ──────────────────────────────────────────────────


class TestFileWalker:
    def test_skips_node_modules_and_pycache(self, agent_mod, tmp_path):
        (tmp_path / "Code" / "x").mkdir(parents=True)
        (tmp_path / "Code" / "x" / "good.py").write_text("# ok",
                                                          encoding="utf-8")
        (tmp_path / "Code" / "x" / "node_modules").mkdir()
        (tmp_path / "Code" / "x" / "node_modules" / "bad.py").write_text(
            "# skip", encoding="utf-8")
        (tmp_path / "Code" / "x" / "__pycache__").mkdir()
        (tmp_path / "Code" / "x" / "__pycache__" / "z.pyc").write_bytes(b"x")
        files = list(agent_mod.ComplianceCrosswalkAgent._iter_scannable_files(
            tmp_path, scan_dirs=["Code"], exts={".py"}))
        names = {f.name for f in files}
        assert "good.py" in names
        assert "bad.py" not in names
        assert "z.pyc" not in names

    def test_extension_filter_respected(self, agent_mod, tmp_path):
        (tmp_path / "Code").mkdir()
        (tmp_path / "Code" / "a.py").write_text("", encoding="utf-8")
        (tmp_path / "Code" / "b.png").write_bytes(b"\x89PNG")
        files = list(agent_mod.ComplianceCrosswalkAgent._iter_scannable_files(
            tmp_path, scan_dirs=["Code"], exts={".py"}))
        suffixes = {f.suffix for f in files}
        assert ".py" in suffixes
        assert ".png" not in suffixes


# ── API key loader ───────────────────────────────────────────────


class TestApiKeyLoader:
    def test_loads_key_from_env_file(self, agent_mod, tmp_path, monkeypatch):
        env = tmp_path / ".env"
        env.write_text("Anthropic_API_KEY=sk-test-12345\nOTHER=value\n",
                       encoding="utf-8")
        monkeypatch.setattr(agent_mod, "ENV_FILE", env)
        assert agent_mod.ComplianceCrosswalkAgent._load_api_key() == "sk-test-12345"

    def test_returns_none_when_env_missing(self, agent_mod, tmp_path,
                                            monkeypatch):
        monkeypatch.setattr(agent_mod, "ENV_FILE", tmp_path / "nope.env")
        assert agent_mod.ComplianceCrosswalkAgent._load_api_key() is None


# ── Pass 2 classifier (Anthropic mocked) ─────────────────────────


class TestClassifier:
    def _make_agent_with_mock_client(self, agent_mod, tmp_path, fake_resp):
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        fake_client = MagicMock()
        fake_client.messages.create.return_value = fake_resp
        agent._client = fake_client
        return agent, fake_client

    def test_classify_file_parses_json_response(self, agent_mod, tmp_path):
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text=json.dumps({
            "file": "x.py",
            "controls": [{"control_id": "SC-8",
                          "evidence": "TLS enforcement",
                          "code_blocks": [{"start_line": 1, "end_line": 5,
                                            "summary": "TLS check"}],
                          "confidence": "high"}]}))]
        fake_resp.usage = MagicMock(input_tokens=100, output_tokens=50,
                                    cache_read_input_tokens=10,
                                    cache_creation_input_tokens=20)
        agent, client = self._make_agent_with_mock_client(
            agent_mod, tmp_path, fake_resp)
        f = tmp_path / "x.py"
        f.write_text("def main():\n    pass\n", encoding="utf-8")
        result = agent._classify_file(f, catalog_context="dummy catalog")
        assert result["controls"][0]["control_id"] == "SC-8"
        assert agent.input_tokens == 100
        assert agent.output_tokens == 50
        assert agent.cache_read_tokens == 10
        assert agent.cache_creation_tokens == 20

    def test_classify_file_skips_oversized_files(self, agent_mod, tmp_path):
        agent, client = self._make_agent_with_mock_client(
            agent_mod, tmp_path, MagicMock())
        f = tmp_path / "big.py"
        f.write_text("x" * (agent_mod.MAX_FILE_BYTES + 1), encoding="utf-8")
        result = agent._classify_file(f, catalog_context="x")
        assert "error" in result
        assert "human review" in result["error"]
        client.messages.create.assert_not_called()

    def test_classify_file_handles_non_json_response(self, agent_mod, tmp_path):
        fake_resp = MagicMock()
        fake_resp.content = [MagicMock(text="not json at all")]
        fake_resp.usage = MagicMock(input_tokens=1, output_tokens=1,
                                    cache_read_input_tokens=0,
                                    cache_creation_input_tokens=0)
        agent, _ = self._make_agent_with_mock_client(
            agent_mod, tmp_path, fake_resp)
        f = tmp_path / "y.py"
        f.write_text("# tiny", encoding="utf-8")
        result = agent._classify_file(f, catalog_context="x")
        assert "error" in result
        assert result["controls"] == []


# ── End-to-end — ComplianceCrossWalk + ComplianceCrosswalkReport ──


class TestPublicSurface:
    def test_complianceCrosswalk_returns_self_for_chaining(self, agent_mod,
                                                            tmp_path):
        oscal = _fake_oscal([
            {"id": "sc-8", "title": "Transmission Confidentiality",
             "parts": [{"name": "statement",
                        "prose": "Protect information in transit."}]}])
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        with _patch_oscal_response(agent_mod, oscal):
            ret = agent.ComplianceCrossWalk(
                file_types=agent_mod.FileType.PYTHON, with_pass2=False)
        assert ret is agent
        assert (tmp_path / "out.json").exists()

    def test_complianceCrosswalkReport_writes_docx(self, agent_mod, tmp_path):
        # Build a minimal SecurityAuditControls.json directly
        doc = {
            "$schema": "x", "collection": "SecurityAuditControls",
            "description": "test", "version": "1", "date": "2026-04-19",
            "frameworks": {
                "nist_800_53": {"name": "x", "source_url": "x",
                                "loaded_from": "x", "control_count": 1},
                "hitrust_csf": {"name": "x", "source": "x",
                                "control_count": 0, "gap": ""}},
            "crosswalk_seed_size": 0, "control_count": 1,
            "controls": [{
                "control_id": "SC-8", "frameworks": ["nist_800_53"],
                "nist_800_53": {"id": "SC-8", "family": "SC",
                                "title": "Transmission", "control_text": "x"},
                "hitrust_csf": None,
                "implementations": [{
                    "source_file": "Code/x.py",
                    "code_blocks": [{"start_line": 1, "end_line": 1,
                                     "summary": "y"}],
                    "evidence": "TLS",
                    "confidence": "high",
                    "classified_by": "agent",
                    "classifier_model": "claude-sonnet-4-6",
                    "classified_at": "2026-04-19T00:00:00+00:00",
                }],
            }],
        }
        ctrl = tmp_path / "SecurityAuditControls.json"
        ctrl.write_text(json.dumps(doc), encoding="utf-8")
        out = tmp_path / "review.docx"
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=ctrl, review_path=out,
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        result = agent.ComplianceCrosswalkReport()
        assert result == out
        assert out.exists()
        assert out.stat().st_size > 1000  # non-trivial .docx

    def test_run_calls_crosswalk_then_report(self, agent_mod, tmp_path):
        oscal = _fake_oscal([
            {"id": "sc-8", "title": "Transmission",
             "parts": [{"name": "statement", "prose": "Protect."}]}])
        agent = agent_mod.ComplianceCrosswalkAgent(
            controls_path=tmp_path / "out.json",
            review_path=tmp_path / "review.docx",
            security_json=tmp_path / "missing.json", api_key="sk-fake")
        with _patch_oscal_response(agent_mod, oscal):
            target = agent.run(file_types=agent_mod.FileType.PYTHON,
                               with_pass2=False)
        assert target == tmp_path / "review.docx"
        assert target.exists()
