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
# Pipeline runtime modules live at pipeline/Code and are imported by
# name (`from notification_client import ...`) rather than as a package.
# Tests need that directory on sys.path so bare imports resolve.
_PIPELINE_CODE = _REPO_ROOT / "pipeline" / "Code"
if _PIPELINE_CODE.is_dir() and str(_PIPELINE_CODE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_CODE))

# Load Code/.env before pytest collects any test module. Several
# pipeline modules read cluster credentials (ATLAS_PUBLIC_KEY,
# ATLAS_PRIVATE_KEY, ATLAS_PROJECT_ID, MONGO_connectionString) at
# module-load time; a test file that imports them (directly or
# transitively) fails collection with KeyError when the env isn't
# populated. The promote test gate runs pytest in its own subprocess
# and the parent shell's env doesn't always propagate through the
# subprocess chain, so loading .env here at collection time is the
# reliable path.
_ENV_FILE = _REPO_ROOT / ".env"
if _ENV_FILE.is_file():
    try:
        from dotenv import load_dotenv as _load_dotenv  # type: ignore[import-untyped]
        _load_dotenv(_ENV_FILE, override=False)
    except ImportError:
        # dotenv is a pipeline runtime dependency; if it is somehow
        # missing here, silently fall through - the collection error
        # from the missing env var will still surface clearly.
        pass

# ChatHealthyLoggingService gracefully skips the Mongo handler when any
# of CH_SPACE_NAME / ENV_PREFIX / MONGO_FRONTEND_connectionString is
# missing (one-shot stderr warning, then stderr/file only). Tests do NOT
# set MONGO_FRONTEND_connectionString: a stub URI would make every log
# call block on serverSelectionTimeoutMS during collection, since
# `.info()` calls are no longer silently dropped in non-debug mode.
os.environ.setdefault("CH_SPACE_NAME", "test-pipeline")
os.environ.setdefault("ENV_PREFIX", "test")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr")

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
