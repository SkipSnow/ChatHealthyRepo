# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLM facade — wraps pydantic-ai Agent.run / Agent.run_sync with the
ChatHealthy retry policy and converts transient HTTP failures into
ChatHealthyException(mode='llm_unavailable').

Realizes EPIC-008-F-002-S-009-REQ-B-007. See
ChatHealthyLib/architectureAndDesign/
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
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
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

# pydantic-ai is imported inside the functions that need it, never at
# module scope. chathealthy_lib/__init__.py imports this module, so a
# top-level import made every consumer of the library depend on it --
# EvaluateCare, which makes no model calls at all, stopped booting on
# ModuleNotFoundError.
def _agent_run_error():
    """The library's base class for a failed run, or None if absent."""
    try:
        from pydantic_ai.exceptions import AgentRunError
    except ImportError:
        return None
    return AgentRunError


def _agent_run_mode(exc: BaseException) -> str:
    """Most specific first: IncompleteToolCall and ContentFilterError both
    subclass UnexpectedModelBehavior."""
    from pydantic_ai.exceptions import (
        ContentFilterError,
        IncompleteToolCall,
        ModelAPIError,
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )
    for exc_type, mode in (
        (IncompleteToolCall, "llm_incomplete_tool_call"),
        (ContentFilterError, "llm_content_filtered"),
        (UnexpectedModelBehavior, "llm_output_retries_exhausted"),
        (UsageLimitExceeded, "llm_usage_limit_exceeded"),
        (ModelAPIError, "llm_provider_error"),
    ):
        if isinstance(exc, exc_type):
            return mode
    return "llm_run_failed"


def raise_agent_run_failure(exc: BaseException, *, provider: str,
                            call_site: str, server: str, component: str,
                            model_name_str: str,
                            messages: list) -> None:
    """Convert a pydantic-ai run failure at the library boundary.

    messages carries the whole exchange -- every attempt, every correction
    fed back to the model, every answer it gave. The exception itself
    carries none of that: on retry exhaustion its message is the string
    "Exceeded maximum output retries (n)" and the model's rejected answers
    are discarded with the run. Without this the operator learns that a
    budget ran out and nothing about what kept failing.
    """
    raise ChatHealthyException(
        mode=_agent_run_mode(exc),
        message=(f"{provider}/{model_name_str} run failed at "
                 f"{call_site}: {type(exc).__name__}: {exc}"),
        server=server,
        component=component,
        provider=provider,
        call_site=call_site,
        exception=exc,
        messages=messages,
    ) from exc


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

    A failure pydantic-ai raises out of the run is converted here too --
    see _AGENT_RUN_MODES. From agent.run forward the failure is ours, and
    no vendor exception type leaves this module.

    Any kwargs beyond call_site/provider/server/component are
    forwarded to agent.run.
    """
    model_name_str = model_name(agent)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        # Holds the exchange even when the run raises, which is the only
        # way to see what the model actually said on a failed run. Scoped
        # to one call: within a single block only the FIRST run's messages
        # are captured, so a shared block across attempts would report the
        # first attempt for all of them.
        from pydantic_ai import capture_run_messages
        with capture_run_messages() as messages:
            try:
                if injected_failure(call_site):
                    raise ChatHealthyException(
                        mode="remote_protocol",
                        component="llm",
                        message="CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure")
                return await agent.run(prompt, **agent_kwargs)
            except TRANSIENT as exc:
                last_exc = exc
                if attempt < MAX_ATTEMPTS:
                    delay = jittered(BACKOFF_SECONDS[attempt - 1])
                    await asyncio.sleep(delay)
                    continue
            except _agent_run_error() as exc:
                # Not retried here. pydantic-ai has already spent whatever
                # budget it was given, and a content filter or a usage cap
                # does not become true on a second ask.
                raise_agent_run_failure(
                    exc, provider=provider, call_site=call_site,
                    server=server, component=component,
                    model_name_str=model_name_str, messages=list(messages))
    assert last_exc is not None
    raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)


def run_llm_sync(agent: Any, prompt: str, *, call_site: str,
                 provider: str, server: str, component: str,
                 **agent_kwargs) -> Any:
    """Sync facade for pydantic-ai Agent.run_sync. Mirrors run_llm; uses
    time.sleep instead of asyncio.sleep. Required by FindCare's
    SpecialtyFilter (filter.py normalize + filter_candidates). See
    run_llm for the meaning of the server and component kwargs.
    """
    model_name_str = model_name(agent)
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        from pydantic_ai import capture_run_messages
        with capture_run_messages() as messages:
            try:
                if injected_failure(call_site):
                    raise ChatHealthyException(
                        mode="remote_protocol",
                        component="llm",
                        message="CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure")
                return agent.run_sync(prompt, **agent_kwargs)
            except TRANSIENT as exc:
                last_exc = exc
                if attempt < MAX_ATTEMPTS:
                    delay = jittered(BACKOFF_SECONDS[attempt - 1])
                    time.sleep(delay)
                    continue
            except _agent_run_error() as exc:
                raise_agent_run_failure(
                    exc, provider=provider, call_site=call_site,
                    server=server, component=component,
                    model_name_str=model_name_str, messages=list(messages))
    assert last_exc is not None
    raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)


EMBEDDING_MODEL_ENV = "CH_EMBEDDING_MODEL"


def embedding_model_name() -> str:
    """The one embedding model, read from the binding the target carries.

    There is one embedding model across the whole application, declared
    once for the firm and bound to every target whose code embeds. There
    is no default: a default would be a second declaration of the value
    and is the thing this reading exists to remove.
    """
    name = os.environ.get(EMBEDDING_MODEL_ENV, "").strip()
    if not name:
        raise ChatHealthyException(
            mode="config_error",
            component="llm",
            message=(f"{EMBEDDING_MODEL_ENV} is not set. The embedding model is "
                     "declared once for the firm and bound to every target whose "
                     "code embeds; this target carries no binding."),
        )
    return name


def embed(text: str, *, call_site: str, provider: str, server: str,
          component: str) -> list:
    """Embeddings facade, on the same ladder as run_llm and run_llm_sync.

    run_llm and run_llm_sync wrap pydantic-ai's Agent, which is an agent
    abstraction and has no embeddings surface, so an embedding call has
    nothing to route through without this. Same MAX_ATTEMPTS, same
    TRANSIENT tuple, same jittered backoff, same raise_unavailable.

    Takes no model argument. The model is the firm's one declaration,
    read from the environment binding the target carries.
    """
    model_name_str = embedding_model_name()
    last_exc: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if injected_failure(call_site):
                raise ChatHealthyException(
                    mode="remote_protocol",
                    component="llm",
                    message="CHATHEALTHY_INJECT_LLM_FAILURE: synthetic failure")
            from openai import OpenAI
            client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
            response = client.embeddings.create(model=model_name_str, input=text)
            return response.data[0].embedding
        except TRANSIENT as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(jittered(BACKOFF_SECONDS[attempt - 1]))
                continue
    assert last_exc is not None
    raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)
