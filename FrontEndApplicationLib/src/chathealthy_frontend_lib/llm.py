# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLM facade — wraps pydantic-ai Agent.run / Agent.run_sync with the
ChatHealthy retry policy and converts transient HTTP failures into
ChatHealthyException(mode='llm_unavailable').

Realizes EPIC-003-F-003-S-001-REQ-B-007. See
FrontEndApplicationLib/architectureAndDesign/
EPIC-003-F-003-Manage-Errors-design-v3.docx §5 for the contract.

Test-only failure injection: when the environment variable
CHATHEALTHY_INJECT_LLM_FAILURE is truthy (non-empty, non-zero), every
attempt raises httpx.RemoteProtocolError so the three-rung ladder can be
exercised end-to-end without a real upstream outage. Tear down by
clearing the variable.
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import time
from typing import Any

import httpx

from .exceptions import ChatHealthyException

_log = logging.getLogger("chathealthy.llm")

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (0.5, 1.5)
_TRANSIENT = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)
_INJECT_ENV = "CHATHEALTHY_INJECT_LLM_FAILURE"


def _injected_failure(call_site: str) -> bool:
    """Test-only failure injection.

    Empty / "0" / "false" / "no" → no injection.
    "1" / "true" / "yes" → inject on every call site.
    Any other non-empty value → treated as a call-site substring match;
        inject only when `call_site` contains it. Lets a test fail the
        classifier without failing the manufacture call.
    """
    raw = os.environ.get(_INJECT_ENV, "")
    if raw in ("", "0", "false", "False", "no", "No"):
        return False
    if raw in ("1", "true", "True", "yes", "Yes"):
        return True
    return raw in call_site


def _model_name(agent: Any) -> str:
    model = getattr(agent, "model", None)
    if model is None:
        return "unknown"
    for attr in ("model_name", "name", "_model_name"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return type(model).__name__


def _jittered(delay: float) -> float:
    return delay * (0.5 + random.random())


def _raise_unavailable(provider: str, call_site: str, server: str,
                       component: str, model_name_str: str,
                       last_exc: Exception) -> None:
    raise ChatHealthyException(
        mode="llm_unavailable",
        message=(f"{provider}/{model_name_str} exhausted after "
                 f"{_MAX_ATTEMPTS} attempts (raised at server={server} "
                 f"component={component})"),
        server=server,
        component=component,
        provider=provider,
        call_site=call_site,
        attempts=_MAX_ATTEMPTS,
        exception=last_exc,
    ) from last_exc


async def run_llm(agent: Any, prompt: str, *, call_site: str,
                  provider: str, server: str, component: str,
                  **agent_kwargs) -> Any:
    """Async facade for pydantic-ai Agent.run with retry on transient
    httpx errors. Raises ChatHealthyException(mode='llm_unavailable')
    when all attempts fail.

    server is the network identity of the process making this LLM call
    (e.g. 'shared_services', 'find_care', 'evaluate_care'). component
    is the logical owner of the call inside that process (e.g. 'UM',
    'SpecialtyFilter'). Both are recorded on every log line and on the
    raised ChatHealthyException as first-class attributes so the
    catching side can identify where the exception originated.

    Any kwargs beyond call_site/provider/server/component are
    forwarded to agent.run.
    """
    model_name_str = _model_name(agent)
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            if _injected_failure(call_site):
                raise httpx.RemoteProtocolError(
                    "CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure"
                )
            return await agent.run(prompt, **agent_kwargs)
        except _TRANSIENT as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                delay = _jittered(_BACKOFF_SECONDS[attempt - 1])
                _log.warning(
                    "llm transient failure attempt %d/%d server=%s "
                    "component=%s call_site=%s provider=%s model=%s "
                    "exc=%s retrying in %.2fs",
                    attempt, _MAX_ATTEMPTS, server, component, call_site,
                    provider, model_name_str, type(exc).__name__, delay,
                )
                await asyncio.sleep(delay)
                continue
            _log.exception(
                "llm exhausted attempts=%d server=%s component=%s "
                "call_site=%s provider=%s model=%s",
                _MAX_ATTEMPTS, server, component, call_site, provider,
                model_name_str,
                stack_info=True,
            )
    assert last_exc is not None
    _raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)


def run_llm_sync(agent: Any, prompt: str, *, call_site: str,
                 provider: str, server: str, component: str,
                 **agent_kwargs) -> Any:
    """Sync facade for pydantic-ai Agent.run_sync. Mirrors run_llm; uses
    time.sleep instead of asyncio.sleep. Required by FindCare's
    SpecialtyFilter (_pick_agent.run_sync, find_specialists.py). See
    run_llm for the meaning of the server and component kwargs.
    """
    model_name_str = _model_name(agent)
    last_exc: Exception | None = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            if _injected_failure(call_site):
                raise httpx.RemoteProtocolError(
                    "CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure"
                )
            return agent.run_sync(prompt, **agent_kwargs)
        except _TRANSIENT as exc:
            last_exc = exc
            if attempt < _MAX_ATTEMPTS:
                delay = _jittered(_BACKOFF_SECONDS[attempt - 1])
                _log.warning(
                    "llm transient failure attempt %d/%d server=%s "
                    "component=%s call_site=%s provider=%s model=%s "
                    "exc=%s retrying in %.2fs",
                    attempt, _MAX_ATTEMPTS, server, component, call_site,
                    provider, model_name_str, type(exc).__name__, delay,
                )
                time.sleep(delay)
                continue
            _log.exception(
                "llm exhausted attempts=%d server=%s component=%s "
                "call_site=%s provider=%s model=%s",
                _MAX_ATTEMPTS, server, component, call_site, provider,
                model_name_str,
                stack_info=True,
            )
    assert last_exc is not None
    _raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)
