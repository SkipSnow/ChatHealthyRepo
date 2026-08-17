# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Test collection names per LLD v22 §9.3 / §9.4."""

from __future__ import annotations

import os

ENV_PREFIX = os.environ.get("ENV_PREFIX", "dev")
TEST_PROVIDER_COLLECTION = os.environ.get(
    "PROVIDER_TEST_COLLECTION",
    "PipelinePublicHealthData.providers_test_v1",
)
TEST_RUNS_COLL = os.environ.get("PIPELINE_TEST_RUNS_COLL", "pipeline_test.runs")
TEST_DISCREPANCIES_COLL = os.environ.get(
    "PIPELINE_TEST_DISCREPANCIES_COLL",
    "pipeline_test.discrepancies",
)
TEST_WORK_ITEMS_COLL = os.environ.get(
    "PIPELINE_TEST_WORK_ITEMS_COLL",
    "pipeline_test.work_items",
)
INTEGRATION_STATES = ["WY", "VT"]
SNAPSHOT_BLOB_PREFIX = "pipeline-sources-test"
