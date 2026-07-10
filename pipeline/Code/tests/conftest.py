# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared pytest fixtures — LLD v22 §9."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def f006_catalog():
    with open(FIXTURES / "f006_catalog.json", encoding="utf-8") as f:
        rows = json.load(f)
    return {str(r.get("Code") or r.get("code")): r for r in rows}


@pytest.fixture
def nppes_individual_row():
    with open(FIXTURES / "nppes_individual_wy.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def nppes_org_row():
    with open(FIXTURES / "nppes_org_vt.json", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def mongo_available():
    try:
        from pipeline_env import load_pipeline_env
        load_pipeline_env()
        from pipeline_db import get_mongo
        get_mongo().admin.command("ping", maxTimeMS=5000)
        return True
    except Exception:
        return False


@pytest.fixture
def integration_enabled():
    return os.environ.get("RUN_PROVIDER_PIPELINE_E2E", "").lower() in ("1", "true", "yes")
