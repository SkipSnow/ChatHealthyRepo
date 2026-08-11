# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
"""Integration test for the UR catch + UM manufacture dispatch on
ChatHealthyException(mode='llm_unavailable').

Realizes verification for EPIC-008-F-002-S-009-REQ-B-007 (UR side):
when UM's classifier/manufacture LLM exhausts retries and raises
ChatHealthyException, UR catches and re-dispatches UM with a synthetic
manufacture trigger carrying the system_llm_unavailable gesture. The
user sees the manufactured prose; no fatal overlay.

Run from sharedServices/Code:
    PYTHONPATH=.;../../ChatHealthyLib/src python -m pytest \\
        tests/test_ur_llm_unavailable_catch.py -v
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from chathealthy_lib import ChatHealthyException


def _make_deps():
    user_object = SimpleNamespace(
        is_locked_out=False,
        intent=None,
        persist_user_state=MagicMock(),
        session_conversation_history=SimpleNamespace(utterances=[]),
        ip_address="127.0.0.1",
    )
    deps = SimpleNamespace(
        user_object=user_object,
        stream=MagicMock(),
    )
    return deps


def test_ur_handle_utterance_catches_llm_unavailable_and_dispatches_manufacture(
    monkeypatch,
):
    from AuthorizationsAndAuthentications import universal_navigation_tool as un

    # First UM call raises; second succeeds. The catch must fire and
    # re-dispatch UM as a manufacture trigger.
    call_log = []

    async def fake_run_and_log(deps, request):
        call_log.append({
            "trigger_type": getattr(request, "trigger_type", None),
            "reason": getattr(request, "manufacture_utterance_reason", None),
        })
        if len(call_log) == 1:
            raise ChatHealthyException(
                mode="llm_unavailable",
                message="gemini exhausted",
                provider="gemini",
                call_site="UM._classifier_agent",
                attempts=3,
            )
        return SimpleNamespace(target_action="closeConnection200")

    fake_um_module = SimpleNamespace(
        TOOL=SimpleNamespace(run_and_log=fake_run_and_log),
        Request=lambda **kw: SimpleNamespace(**{
            "trigger_type": kw.get("trigger_type", "utterance"),
            "manufacture_utterance_reason": kw.get(
                "manufacture_utterance_reason", {}
            ),
        }),
    )

    monkeypatch.setitem(
        __import__("sys").modules,
        "UtteranceManager.utterance_manager",
        fake_um_module,
    )
    import sys
    sys.modules["UtteranceManager"] = SimpleNamespace(manager=fake_um_module)

    tool = un.UniversalNavigationTool()
    deps = _make_deps()
    # Bypass the lockout pre-check (no Mongo in this test).
    tool._hydrate_lockout_if_any = AsyncMock()

    resp = asyncio.run(tool._handle_utterance(deps, {"text": "any utterance"}))

    assert len(call_log) == 2
    assert call_log[0]["trigger_type"] == "utterance"
    assert call_log[1]["trigger_type"] == "manufacture"
    reason = call_log[1]["reason"]
    assert reason["gesture"] == "system_llm_unavailable"
    assert reason["provider"] == "gemini"
    assert reason["call_site"] == "UM._classifier_agent"
    assert reason["attempts"] == 3
    assert resp.result["mode"] == "llm_unavailable"
    assert resp.result["target_action"] == "closeConnection200"


def test_ur_handle_utterance_propagates_non_llm_unavailable_modes(
    monkeypatch,
):
    """Any mode other than llm_unavailable falls through to fatal (rung 3)."""
    from AuthorizationsAndAuthentications import universal_navigation_tool as un

    async def fake_run_and_log(deps, request):
        raise ChatHealthyException(
            mode="compliance_violation",
            message="phi in logs",
        )

    fake_um_module = SimpleNamespace(
        TOOL=SimpleNamespace(run_and_log=fake_run_and_log),
        Request=lambda **kw: SimpleNamespace(**{
            "trigger_type": kw.get("trigger_type", "utterance"),
            "manufacture_utterance_reason": kw.get(
                "manufacture_utterance_reason", {}
            ),
        }),
    )
    import sys
    sys.modules["UtteranceManager"] = SimpleNamespace(manager=fake_um_module)
    sys.modules["UtteranceManager.utterance_manager"] = fake_um_module

    tool = un.UniversalNavigationTool()
    deps = _make_deps()
    tool._hydrate_lockout_if_any = AsyncMock()

    with pytest.raises(ChatHealthyException) as ei:
        asyncio.run(tool._handle_utterance(deps, {"text": "any utterance"}))
    assert ei.value.mode == "compliance_violation"
