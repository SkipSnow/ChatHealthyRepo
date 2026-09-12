# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared pytest fixtures — LLD v22 §9."""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

# The chathealthy_lib package lives in a peer directory and is
# not pip-installed for the pipeline runtime; tests need it on sys.path
# so imports of ChatHealthyLoggingService / ChatHealthyException /
# ChatHealthyMongoUtilities resolve during collection.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_LIB_SRC = _REPO_ROOT / "ChatHealthyLib" / "src"
if _LIB_SRC.is_dir() and str(_LIB_SRC) not in sys.path:
    sys.path.insert(0, str(_LIB_SRC))
# Pipeline runtime modules live at pipeline/Code and are imported by
# name (`from chathealthy_lib.notification_client import ...`) rather than as a package.
# Tests need that directory on sys.path so bare imports resolve.
_PIPELINE_CODE = _REPO_ROOT / "pipeline" / "Code"
if _PIPELINE_CODE.is_dir() and str(_PIPELINE_CODE) not in sys.path:
    sys.path.insert(0, str(_PIPELINE_CODE))

# Load Code/.env before pytest collects any test module. Several
# pipeline modules read cluster credentials (ATLAS_PUBLIC_KEY,
# ATLAS_PRIVATE_KEY, ATLAS_PROJECT_ID) at
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

# ChatHealthyLoggingService skips the Mongo handler when CH_SPACE_NAME
# or ENV_PREFIX is missing (one-shot stderr warning, then stderr/file
# only). Both are set below so log calls during collection stay local.
os.environ.setdefault("CH_SPACE_NAME", "test-pipeline")
os.environ.setdefault("ENV_PREFIX", "test")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr")

import pytest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def f006_catalog():
    with open(FIXTURES / "f006_catalog.jsonfixture", encoding="utf-8") as f:
        rows = json.load(f)
    return {str(r.get("Code") or r.get("code")): r for r in rows}


@pytest.fixture
def nppes_individual_row():
    with open(FIXTURES / "nppes_individual_wy.jsonfixture", encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def nppes_org_row():
    with open(FIXTURES / "nppes_org_vt.jsonfixture", encoding="utf-8") as f:
        return json.load(f)


# The one database DevOpsUser holds readWrite on. Scratch collections
# live here because that grant already exists — a test does not get to
# invent a permission for its own convenience, and no new namespace is
# created on any cluster to run one.
SCRATCH_DB = "frontEndAdmin"
SCRATCH_PREFIX = "chathealthy_test_scratch_"


@pytest.fixture
def scratch_mongo():
    """A real MongoDB database and disposable, uniquely-named collections.

    The rule is absolute: tests reach MongoDB through the canonical
    utility as DevOpsUser, presenting the certificate that IS the
    credential. A fake in-memory Mongo proves nothing about the
    behaviours these tests assert — what the server does to an array
    under a concurrent $set, what renameCollection means — and it
    exercises no part of the identity model.

    Isolation is by collection name. Every collection this hands out
    carries a prefix and a fresh uuid, so it cannot collide with a
    production collection, and every one is dropped afterwards whether
    the test passed or failed.

    Yields (database, collection_factory). The factory takes a short
    label and returns a real collection.
    """
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

    client = ChatHealthyMongoUtilities().getConnection("DevOpsUser", "ChatHealthyDataPipelines")
    db = client[SCRATCH_DB]
    run_prefix = f"{SCRATCH_PREFIX}{uuid.uuid4().hex}_"

    def collection(label: str):
        return db[f"{run_prefix}{label}"]

    try:
        yield db, collection
    finally:
        # Drop by prefix, not by what was handed out. Code under test
        # derives further names from the ones it is given — a version
        # suffix, a staging twin — and every one of those carries the
        # run prefix, so this is what actually leaves the cluster clean.
        for name in db.list_collection_names():
            if name.startswith(run_prefix):
                db.drop_collection(name)


@pytest.fixture(scope="session")
def mongo_available():
    """Asked once per session, not once per test.

    The pipeline cluster is paused whenever no run holds a reservation, so
    this probe usually fails, and connecting costs the connect retry each
    time it is asked. Per-test that is minutes of the suite spent learning
    the same answer repeatedly.
    """
    try:
        from pipeline_env import load_pipeline_env
        from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
        load_pipeline_env()
        ChatHealthyMongoUtilities().getConnection("pipelineEditor", "ChatHealthyDataPipelines").admin.command("ping", maxTimeMS=5000)
        return True
    except Exception:
        return False


@pytest.fixture
def integration_enabled():
    return os.environ.get("RUN_PROVIDER_PIPELINE_E2E", "").lower() in ("1", "true", "yes")
