# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLM facade — wraps pydantic-ai Agent.run / Agent.run_sync with the
ChatHealthy retry policy and converts transient HTTP failures into
ChatHealthyException(mode='llm_unavailable').

Realizes EPIC-008-F-002-S-009-REQ-B-007. See
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
from .logging_service import ChatHealthyLoggingService
import os
import random
import time
from typing import Any

import httpx

from .exceptions import ChatHealthyException

log = ChatHealthyLoggingService()
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (0.5, 1.5)
TRANSIENT = (
    httpx.RemoteProtocolError,
    httpx.ConnectError,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
)
INJECT_ENV = "CHATHEALTHY_INJECT_LLM_FAILURE"


def injected_failure(call_site: str) -> bool:
    """Test-only failure injection.

    Empty / "0" / "false" / "no" → no injection.
    "1" / "true" / "yes" → inject on every call site.
    Any other non-empty value → treated as a call-site substring match;
        inject only when `call_site` contains it. Lets a test fail the
        classifier without failing the manufacture call.
    """
    raw = os.environ.get(INJECT_ENV, "")
    if raw in ("", "0", "false", "False", "no", "No"):
        return False
    if raw in ("1", "true", "True", "yes", "Yes"):
        return True
    return raw in call_site


def model_name(agent: Any) -> str:
    model = getattr(agent, "model", None)
    if model is None:
        return "unknown"
    for attr in ("model_name", "name", "_model_name"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    return type(model).__name__


def jittered(delay: float) -> float:
    return delay * (0.5 + random.random())


def raise_unavailable(provider: str, call_site: str, server: str,
                       component: str, model_name_str: str,
                       last_exc: Exception) -> None:
    raise ChatHealthyException(
        mode="llm_unavailable",
        message=(f"{provider}/{model_name_str} exhausted after "
                 f"{MAX_ATTEMPTS} attempts (raised at server={server} "
                 f"component={component})"),
        server=server,
        component=component,
        provider=provider,
        call_site=call_site,
        attempts=MAX_ATTEMPTS,
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
    model_name_str = model_name(agent)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if injected_failure(call_site):
                raise httpx.RemoteProtocolError(
                    "CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure"
                )
            return await agent.run(prompt, **agent_kwargs)
        except TRANSIENT as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                delay = jittered(BACKOFF_SECONDS[attempt - 1])
                log.warning(
                    "llm transient failure attempt %d/%d server=%s "
                    "component=%s call_site=%s provider=%s model=%s "
                    "exc=%s retrying in %.2fs",
                    attempt, MAX_ATTEMPTS, server, component, call_site,
                    provider, model_name_str, type(exc).__name__, delay,
                    exc=ChatHealthyException(
                        mode="llm_transient_retrying",
                        message=(
                            f"llm transient failure attempt {attempt}/{MAX_ATTEMPTS} "
                            f"server={server} component={component} call_site={call_site} "
                            f"provider={provider} model={model_name_str} "
                            f"exc={type(exc).__name__}"
                        ),
                        server=server,
                        component=component,
                        provider=provider,
                        call_site=call_site,
                        attempts=attempt,
                        exception=exc,
                    ),
                    if_not_debug_log=True,
                )
                await asyncio.sleep(delay)
                continue
            log.exception(
                "llm exhausted attempts=%d server=%s component=%s "
                "call_site=%s provider=%s model=%s",
                MAX_ATTEMPTS, server, component, call_site, provider,
                model_name_str,
                exc=ChatHealthyException(
                    mode="llm_exhausted",
                    message=(
                        f"llm exhausted attempts={MAX_ATTEMPTS} "
                        f"server={server} component={component} call_site={call_site} "
                        f"provider={provider} model={model_name_str} "
                        f"exc={type(exc).__name__}"
                    ),
                    server=server,
                    component=component,
                    provider=provider,
                    call_site=call_site,
                    attempts=attempt,
                    exception=exc,
                ),
                if_not_debug_log=True,
            )
    assert last_exc is not None
    raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)


def run_llm_sync(agent: Any, prompt: str, *, call_site: str,
                 provider: str, server: str, component: str,
                 **agent_kwargs) -> Any:
    """Sync facade for pydantic-ai Agent.run_sync. Mirrors run_llm; uses
    time.sleep instead of asyncio.sleep. Required by FindCare's
    SpecialtyFilter (_pick_agent.run_sync, find_specialists.py). See
    run_llm for the meaning of the server and component kwargs.
    """
    model_name_str = model_name(agent)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if injected_failure(call_site):
                raise httpx.RemoteProtocolError(
                    "CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure"
                )
            return agent.run_sync(prompt, **agent_kwargs)
        except TRANSIENT as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                delay = jittered(BACKOFF_SECONDS[attempt - 1])
                log.warning(
                    "llm transient failure attempt %d/%d server=%s "
                    "component=%s call_site=%s provider=%s model=%s "
                    "exc=%s retrying in %.2fs",
                    attempt, MAX_ATTEMPTS, server, component, call_site,
                    provider, model_name_str, type(exc).__name__, delay,
                    exc=ChatHealthyException(
                        mode="llm_transient_retrying",
                        message=(
                            f"llm transient failure attempt {attempt}/{MAX_ATTEMPTS} "
                            f"server={server} component={component} call_site={call_site} "
                            f"provider={provider} model={model_name_str} "
                            f"exc={type(exc).__name__}"
                        ),
                        server=server,
                        component=component,
                        provider=provider,
                        call_site=call_site,
                        attempts=attempt,
                        exception=exc,
                    ),
                    if_not_debug_log=True,
                )
                time.sleep(delay)
                continue
            log.exception(
                "llm exhausted attempts=%d server=%s component=%s "
                "call_site=%s provider=%s model=%s",
                MAX_ATTEMPTS, server, component, call_site, provider,
                model_name_str,
                exc=ChatHealthyException(
                    mode="llm_exhausted",
                    message=(
                        f"llm exhausted attempts={MAX_ATTEMPTS} "
                        f"server={server} component={component} call_site={call_site} "
                        f"provider={provider} model={model_name_str} "
                        f"exc={type(exc).__name__}"
                    ),
                    server=server,
                    component=component,
                    provider=provider,
                    call_site=call_site,
                    attempts=attempt,
                    exception=exc,
                ),
                if_not_debug_log=True,
            )
    assert last_exc is not None
    raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)
