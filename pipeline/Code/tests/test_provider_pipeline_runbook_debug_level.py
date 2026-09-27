"""debug_level resolution and propagation in the provider pipeline runbook.

Grounds in EPIC-010-F-001-S-002-REQ-B-001: a run's behaviour comes from its
invocation arguments, not from values in code. debug_level replaces the
hardcoded DEBUG log level; the runbook resolves it from the webhook body and
injects it into both container docker runs via CH_LOG_LEVEL, which the
controller and every worker read through ChatHealthyLoggingService.
"""
import base64
import os
import sys
import types
from pathlib import Path

import pytest

# The runbook lives in pipeline/deploy, off the harness's default path, and
# does import-time work: a pip-ensure block keyed on the wrong import names
# (azure_identity, azure_keyvault_secrets) and a hard requirement for the
# pipelineEditor credential. Satisfy both before the import so the module
# loads without a network pip round-trip or a credential raise.
for _name in ("azure_identity", "azure_keyvault_secrets"):
    sys.modules.setdefault(_name, types.ModuleType(_name))
os.environ.setdefault("PIPELINEEDITOR_AZURE_TENANT_ID", "test-tenant")
os.environ.setdefault("PIPELINEEDITOR_AZURE_CLIENT_ID", "test-client")
os.environ.setdefault("PIPELINEEDITOR_AZURE_CLIENT_SECRET", "test-secret")

_DEPLOY_DIR = Path(__file__).resolve().parents[2] / "deploy"
if str(_DEPLOY_DIR) not in sys.path:
    sys.path.insert(0, str(_DEPLOY_DIR))

import provider_pipeline_runbook as rb  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402


def test_resolve_accepts_both_spellings():
    assert rb._resolve_debug_level({"debug_level": "WARNING"}) == "WARNING"
    assert rb._resolve_debug_level({"debuglevel": "ERROR"}) == "ERROR"


def test_resolve_prefers_debug_level_when_both_present():
    body = {"debug_level": "WARNING", "debuglevel": "ERROR"}
    assert rb._resolve_debug_level(body) == "WARNING"


def test_resolve_defaults_to_info_when_absent():
    # Deliberate behaviour change from today's always-DEBUG: a plain
    # production fire runs at INFO.
    assert rb._resolve_debug_level({}) == "INFO"
    assert rb._resolve_debug_level(None) == "INFO"
    assert rb.DEBUG_LEVEL_DEFAULT == "INFO"


def test_resolve_normalises_case_and_whitespace():
    assert rb._resolve_debug_level({"debug_level": "debug"}) == "DEBUG"
    assert rb._resolve_debug_level({"debug_level": "  info  "}) == "INFO"
    assert rb._resolve_debug_level({"debuglevel": "Critical"}) == "CRITICAL"


def test_resolve_rejects_bad_value():
    with pytest.raises(ChatHealthyException) as ei:
        rb._resolve_debug_level({"debug_level": "verbose"})
    assert ei.value.mode == "value_error"


def _render(debug_level):
    return base64.b64decode(
        rb._cloud_init_user_data(
            run_id="prov-2026-01-01T00-00-00Z-abcdef",
            load_mode="full",
            state_scope=["DE"],
            invocation_mode="webhook",
            resume_from_step="",
            data_version=5,
            google_maps_enabled=False,
            debug_level=debug_level,
        )
    ).decode("utf-8")


def test_cloud_init_injects_chosen_level_into_both_docker_runs():
    rendered = _render("WARNING")
    marker = "-e CH_LOG_LEVEL='WARNING'"
    # Main controller run AND the failure-report run both carry it.
    assert rendered.count(marker) == 2
    # The main controller run is the one preceding the DOCKER_EXIT capture;
    # confirm the level lands in that run specifically.
    main_run = rendered.split("DOCKER_EXIT=$?")[0]
    assert marker in main_run
    assert "CH_LOG_LEVEL='DEBUG'" not in rendered


def test_cloud_init_defaults_to_info_when_level_omitted():
    rendered = base64.b64decode(
        rb._cloud_init_user_data(
            run_id="prov-2026-01-01T00-00-00Z-abcdef",
            load_mode="full",
            state_scope=["DE"],
            invocation_mode="webhook",
            resume_from_step="",
            data_version=5,
        )
    ).decode("utf-8")
    assert rendered.count("-e CH_LOG_LEVEL='INFO'") == 2
