from unittest.mock import MagicMock, patch

import pytest

from step_context import PipelineArgs, RunManifest, StepContext
from steps.source_freshness_gate import run_step


@pytest.mark.unit
def test_freshness_gate():
    args = PipelineArgs(states=["WY"], env_prefix="dev", data_version=3)
    manifest = RunManifest.new("provider", args)
    ctx = StepContext(
        args=args,
        manifest=manifest,
        config={
            "source_freshness": [
                {"source_name": "nppes_npi", "number_of_days": 30},
                {"source_name": "census_zcta_county", "number_of_days": 365},
                {"source_name": "usda_rucc", "number_of_days": 365},
                {"source_name": "nucc", "number_of_days": 90},
            ]
        },
        mongo_client=MagicMock(),
        blob_client=None,
    )
    registry = MagicMock()
    registry.find_one.return_value = {}
    fake_mongo = MagicMock()
    fake_mongo.__getitem__.return_value = registry
    with patch("pipeline_db.get_mongo", return_value=fake_mongo):
        out = run_step(ctx)
    assert len(out["decisions"]) == 5


@pytest.mark.unit
def test_always_refetch_bypasses_probe(monkeypatch):
    """Entry with always_refetch=true short-circuits to decision=fetch
    with the always_refetch reason -- no HEAD probe fired."""
    args = PipelineArgs(states=["VT"], env_prefix="dev", data_version=3)
    manifest = RunManifest.new("provider", args)
    ctx = StepContext(
        args=args,
        manifest=manifest,
        config={
            "source_freshness": [
                {"source_name": "specialty_catalog", "always_refetch": True},
            ],
        },
        mongo_client=MagicMock(),
        blob_client=None,
    )
    monkeypatch.setenv("SPECIALTY_CLASSIFICATION_CATALOG_URL", "https://example/x.json")
    registry = MagicMock()
    registry.find_one.return_value = {}
    fake_mongo = MagicMock()
    fake_mongo.__getitem__.return_value = registry
    # If the probe were called, this would fail (no such URL). The test
    # passes iff always_refetch short-circuits BEFORE the probe.
    with patch("pipeline_db.get_mongo", return_value=fake_mongo), \
         patch("steps.source_freshness_gate.probe_source_version",
               side_effect=AssertionError("probe MUST NOT be called")):
        out = run_step(ctx)
    dec = out["decisions"]["specialty_catalog"]
    assert dec["decision"] == "fetch"
    assert dec["reason"] == "always_refetch=true in pipeline.config"
