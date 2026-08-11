# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for pipeline_config.load_pipeline_config.

The loader reads brain/machine_artifacts/content/pipeline_config.json
and returns the record whose env matches. No fallbacks: missing file,
malformed JSON, or missing env-record raises ChatHealthyException.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from chathealthy_lib.exceptions import ChatHealthyException

import pipeline_config as pc


def _write(tmp_path: Path, payload) -> Path:
    """Write payload as JSON to a temp path and return it."""
    p = tmp_path / "pipeline_config.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.mark.unit
def test_load_returns_matching_env_record(tmp_path, monkeypatch):
    payload = {
        "$schema": "x",
        "pipeline_config": [
            {"env": "dev",  "batch_limits": {}, "dataset_versions": {},
             "failure_thresholds": {}, "source_freshness": []},
            {"env": "prod", "batch_limits": {}, "dataset_versions": {},
             "failure_thresholds": {}, "source_freshness": [{"source_name": "x"}]},
        ],
    }
    monkeypatch.setattr(pc, "_CONFIG_PATH", _write(tmp_path, payload))
    out = pc.load_pipeline_config(env_prefix="prod")
    assert out["env"] == "prod"
    assert out["source_freshness"][0]["source_name"] == "x"


@pytest.mark.unit
def test_load_raises_when_file_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(pc, "_CONFIG_PATH", tmp_path / "nonexistent.json")
    with pytest.raises(ChatHealthyException) as exc_info:
        pc.load_pipeline_config(env_prefix="dev")
    assert exc_info.value.mode == "pipeline_config_missing"


@pytest.mark.unit
def test_load_raises_on_malformed_json(tmp_path, monkeypatch):
    p = tmp_path / "pipeline_config.json"
    p.write_text("{ not valid json", encoding="utf-8")
    monkeypatch.setattr(pc, "_CONFIG_PATH", p)
    with pytest.raises(ChatHealthyException) as exc_info:
        pc.load_pipeline_config(env_prefix="dev")
    assert exc_info.value.mode == "pipeline_config_malformed_json"


@pytest.mark.unit
def test_load_raises_when_env_not_present(tmp_path, monkeypatch):
    payload = {"$schema": "x", "pipeline_config": [
        {"env": "dev", "batch_limits": {}, "dataset_versions": {},
         "failure_thresholds": {}, "source_freshness": []},
    ]}
    monkeypatch.setattr(pc, "_CONFIG_PATH", _write(tmp_path, payload))
    with pytest.raises(ChatHealthyException) as exc_info:
        pc.load_pipeline_config(env_prefix="qa")
    assert exc_info.value.mode == "pipeline_config_env_not_found"


@pytest.mark.unit
def test_shipped_brain_json_loads_all_declared_envs():
    """The committed brain file must parse and expose dev/qa/prod records
    with the fields every reader in pipeline/Code counts on."""
    for env in ("dev", "qa", "prod"):
        rec = pc.load_pipeline_config(env_prefix=env)
        assert rec["env"] == env
        assert "normalize_batch_size" in rec["batch_limits"]
        # dataset_versions is a list of dataset entries per registry
        # contract; provider MUST be one of them.
        dv = rec["dataset_versions"]
        assert isinstance(dv, list) and dv, "dataset_versions must be a non-empty list"
        source_names = {e["source_name"] for e in dv if isinstance(e, dict)}
        assert "provider" in source_names
        assert "discrepancy_abort" in rec["failure_thresholds"]
        names = {s["source_name"] for s in rec["source_freshness"]}
        assert "specialty_catalog" in names
        cat = next(s for s in rec["source_freshness"] if s["source_name"] == "specialty_catalog")
        assert cat.get("always_refetch") is True
