# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from __future__ import annotations

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


def test_partition_filter_state():
    # Operator directive 2026-08-03: PipelineRuntime no longer opens a
    # frontend-mongo connection; all coord lives on the pipeline cluster
    # via ctx.mongo_client. No monkeypatch needed.
    args = PipelineArgs(states=["WY"], env_prefix="dev", data_version=3)
    manifest = RunManifest(run_id="r1", pipeline_name="provider", env_prefix="dev")
    ctx = StepContext(args=args, manifest=manifest, config={}, mongo_client=_Mongo(), blob_client=None)
    rt = PipelineRuntime(ctx)
    filt = rt.partition_filter("WY")
    # NPI-atomic ownership: partition matches on the single-valued
    # business (mailing) address, per LLD. Business address is required
    # by NPPES and unique per NPI, so $elemMatch gives exactly one owner.
    assert filt == {
        "run_id": "r1",
        "business_address.state": "WY",
    }
