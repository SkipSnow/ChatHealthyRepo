# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""pytest plumbing for find_care_smoke_test.py (sibling module).

Adds the --smoke-env CLI option so the master smoke test can be invoked by the
deploy classes (LocalDeploy, RemoteDeploy) with a specific environment per
V11 S-006 (smoke test is parameterized on env={local,dev,qa,prod}).

The find_care_smoke_test module reads SMOKE_TEST_ENV at import time, so this
conftest sets the env var in pytest_configure (which runs before collection).
"""
import os


def pytest_addoption(parser):
    parser.addoption(
        "--smoke-env",
        action="store",
        default=None,
        choices=["local", "dev", "qa", "prod"],
        help=(
            "Target environment for the master smoke test. "
            "Maps to SMOKE_TEST_ENV; if omitted, the env var is honored."
        ),
    )


def pytest_configure(config):
    # Register markers so they don't trigger PytestUnknownMarkWarning.
    config.addinivalue_line(
        "markers",
        "mtls_required: test requires mTLS to be enabled in the env "
        "(local only; HF-deployed envs do not support mTLS). "
        "Deploy classes filter this marker via -m 'not mtls_required' for "
        "non-local envs (BUG-003 item #5)."
    )
    val = config.getoption("--smoke-env")
    if val:
        # Set BEFORE any test module is imported so module-level reads of
        # SMOKE_TEST_ENV pick up the right value.
        os.environ["SMOKE_TEST_ENV"] = val
