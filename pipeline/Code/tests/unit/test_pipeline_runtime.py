# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from __future__ import annotations

from unittest.mock import patch

from step_context import PipelineArgs, RunManifest, StepContext
from pipeline_runtime import PipelineRuntime


class _Mongo:
    def __getitem__(self, _name):
        return self

    def __getattr__(self, _name):
        return self

    def find(self, *_a, **_k):
        return []

    def find_one(self, *_a, **_k):
        return None


@patch("pipeline_runtime.get_frontend_mongo", return_value=_Mongo())
def test_partition_filter_state(_mock_frontend):
    args = PipelineArgs(states=["WY"], env_prefix="dev", data_version=3)
    manifest = RunManifest(run_id="r1", pipeline_name="provider", env_prefix="dev")
    ctx = StepContext(args=args, manifest=manifest, config={}, mongo_client=_Mongo(), blob_client=None)
    rt = PipelineRuntime(ctx)
    filt = rt.partition_filter("WY")
    assert filt == {"run_id": "r1", "addresses": {"$elemMatch": {"state": "WY"}}}
