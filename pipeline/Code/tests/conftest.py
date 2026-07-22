# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared pytest fixtures — LLD v22 §9."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# The chathealthy_frontend_lib package lives in a peer directory and is
# not pip-installed for the pipeline runtime; tests need it on sys.path
# so imports of ChatHealthyLoggingService / ChatHealthyException /
# ChatHealthyMongoUtilities resolve during collection.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_SRC = _REPO_ROOT / "FrontEndApplicationLib" / "src"
if _LIB_SRC.is_dir() and str(_LIB_SRC) not in sys.path:
    sys.path.insert(0, str(_LIB_SRC))

# ChatHealthyLoggingService raises at first log emit if any of
# CH_SPACE_NAME / ENV_PREFIX / MONGO_FRONTEND_connectionString is missing
# ("if you can't log to Mongo you die"). Tests do not touch Mongo; stub
# values so the logger builds a handler without hitting the abort path.
# The MongoLogHandler's emit is lazy so no real connection attempt is
# made until a test actually logs.
os.environ.setdefault("CH_SPACE_NAME", "test-pipeline")
os.environ.setdefault("ENV_PREFIX", "test")
os.environ.setdefault(
    "MONGO_FRONTEND_connectionString",
    "mongodb://test-stub:27017/?appName=test-pipeline",
)

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
