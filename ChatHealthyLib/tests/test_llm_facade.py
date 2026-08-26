# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
"""Unit tests for chathealthy_lib.llm.run_llm / run_llm_sync.

Realizes verification for EPIC-008-F-002-S-009-REQ-B-007.

Run from ChatHealthyLib/ with:
    PYTHONPATH=src python -m pytest tests/test_llm_facade.py -v
"""
from __future__ import annotations

import asyncio
import os
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from chathealthy_lib import (
    ChatHealthyException,
    run_llm,
    run_llm_sync,
)
from chathealthy_lib import llm as llm_mod


@pytest.fixture(autouse=True)
def _fast_backoff(monkeypatch):
    monkeypatch.setattr(llm_mod, "BACKOFF_SECONDS", (0.0, 0.0))
    monkeypatch.delenv(llm_mod.INJECT_ENV, raising=False)


def _fake_agent_async(side_effects):
    agent = SimpleNamespace()
    agent.model = SimpleNamespace(model_name="gemini-2.5-flash")
    agent.run = AsyncMock(side_effect=side_effects)
    agent.run_sync = MagicMock()
    return agent


def _fake_agent_sync(side_effects):
    agent = SimpleNamespace()
    agent.model = SimpleNamespace(model_name="gemini-2.5-flash")
    agent.run = AsyncMock()
    agent.run_sync = MagicMock(side_effect=side_effects)
    return agent


# ── async ────────────────────────────────────────────────────────────


def test_run_llm_returns_when_first_attempt_succeeds():
    agent = _fake_agent_async([SimpleNamespace(data="ok")])
    result = asyncio.run(run_llm(
        agent, "hi", call_site="test.succeed", provider="gemini",
        server="test_server", component="test_component",
    ))
    assert result.data == "ok"
    assert agent.run.call_count == 1


def test_run_llm_retries_on_transient_then_succeeds():
    agent = _fake_agent_async([
        httpx.RemoteProtocolError("disconnect"),
        httpx.ConnectError("conn"),
        SimpleNamespace(data="ok"),
    ])
    result = asyncio.run(run_llm(
        agent, "hi", call_site="test.retry_then_succeed", provider="gemini",
        server="test_server", component="test_component",
    ))
    assert result.data == "ok"
    assert agent.run.call_count == 3


def test_run_llm_raises_chathealthy_after_three_failures():
    agent = _fake_agent_async([
        httpx.RemoteProtocolError("a"),
        httpx.RemoteProtocolError("b"),
        httpx.RemoteProtocolError("c"),
    ])
    with pytest.raises(ChatHealthyException) as ei:
        asyncio.run(run_llm(
            agent, "hi", call_site="test.exhaust", provider="gemini",
            server="test_server", component="test_component",
        ))
    exc = ei.value
    assert exc.mode == "llm_unavailable"
    assert exc.server == "test_server"
    assert exc.component == "test_component"
    assert exc.context["provider"] == "gemini"
    assert exc.context["call_site"] == "test.exhaust"
    assert exc.context["attempts"] == 3
    assert isinstance(exc.exception, httpx.RemoteProtocolError)
    assert agent.run.call_count == 3


def test_run_llm_does_not_retry_on_non_transient():
    agent = _fake_agent_async([ValueError("not transient")])
    with pytest.raises(ValueError):
        asyncio.run(run_llm(
            agent, "hi", call_site="test.non_transient", provider="gemini",
            server="test_server", component="test_component",
        ))
    assert agent.run.call_count == 1


def test_run_llm_inject_env_forces_failure(monkeypatch):
    monkeypatch.setenv(llm_mod.INJECT_ENV, "1")
    agent = _fake_agent_async([SimpleNamespace(data="never_called")])
    with pytest.raises(ChatHealthyException) as ei:
        asyncio.run(run_llm(
            agent, "hi", call_site="test.inject", provider="gemini",
            server="test_server", component="test_component",
        ))
    assert ei.value.mode == "llm_unavailable"
    assert agent.run.call_count == 0


def test_run_llm_inject_env_falsey_does_not_force(monkeypatch):
    monkeypatch.setenv(llm_mod.INJECT_ENV, "0")
    agent = _fake_agent_async([SimpleNamespace(data="ok")])
    result = asyncio.run(run_llm(
        agent, "hi", call_site="test.inject_off", provider="gemini",
        server="test_server", component="test_component",
    ))
    assert result.data == "ok"


# ── sync ─────────────────────────────────────────────────────────────


def test_run_llm_sync_returns_when_first_attempt_succeeds():
    agent = _fake_agent_sync([SimpleNamespace(data="ok")])
    result = run_llm_sync(
        agent, "hi", call_site="test.sync.succeed", provider="gemini",
        server="test_server", component="test_component",
    )
    assert result.data == "ok"
    assert agent.run_sync.call_count == 1


def test_run_llm_sync_retries_then_succeeds():
    agent = _fake_agent_sync([
        httpx.ReadTimeout("t"),
        SimpleNamespace(data="ok"),
    ])
    result = run_llm_sync(
        agent, "hi", call_site="test.sync.retry", provider="gemini",
        server="test_server", component="test_component",
    )
    assert result.data == "ok"
    assert agent.run_sync.call_count == 2


def test_run_llm_sync_raises_chathealthy_after_three_failures():
    agent = _fake_agent_sync([
        httpx.PoolTimeout("a"),
        httpx.PoolTimeout("b"),
        httpx.PoolTimeout("c"),
    ])
    with pytest.raises(ChatHealthyException) as ei:
        run_llm_sync(
            agent, "hi", call_site="test.sync.exhaust", provider="gemini",
            server="test_server", component="test_component",
        )
    exc = ei.value
    assert exc.mode == "llm_unavailable"
    assert exc.context["attempts"] == 3
    assert isinstance(exc.exception, httpx.PoolTimeout)
    assert agent.run_sync.call_count == 3


def test_run_llm_sync_inject_env(monkeypatch):
    monkeypatch.setenv(llm_mod.INJECT_ENV, "yes")
    agent = _fake_agent_sync([SimpleNamespace(data="never")])
    with pytest.raises(ChatHealthyException) as ei:
        run_llm_sync(
            agent, "hi", call_site="test.sync.inject", provider="gemini",
            server="test_server", component="test_component",
        )
    assert ei.value.mode == "llm_unavailable"
    assert agent.run_sync.call_count == 0


# ── exception class ──────────────────────────────────────────────────


def test_chathealthy_exception_is_catchable_as_exception():
    try:
        raise ChatHealthyException(mode="llm_unavailable", message="x")
    except Exception as exc:
        assert isinstance(exc, ChatHealthyException)


def test_chathealthy_exception_context_excludes_exception_attr():
    underlying = httpx.RemoteProtocolError("u")
    exc = ChatHealthyException(
        mode="llm_unavailable",
        message="m",
        provider="gemini",
        exception=underlying,
    )
    assert exc.exception is underlying
    assert "exception" not in exc.context
    assert exc.context == {"provider": "gemini"}
    assert exc.server is None
    assert exc.component is None


def test_chathealthy_exception_lifts_server_and_component_to_attrs():
    exc = ChatHealthyException(
        mode="llm_unavailable",
        message="m",
        server="find_care",
        component="SpecialtyFilter",
        provider="gemini",
    )
    assert exc.server == "find_care"
    assert exc.component == "SpecialtyFilter"
    assert "server" not in exc.context
    assert "component" not in exc.context
    assert exc.context == {"provider": "gemini"}
