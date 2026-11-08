# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""What a model call needs that is not the call itself.

A caller reaches its model through a pydantic_ai.Agent directly. This module
holds the surrounding facts, so each call site does not restate them:

  raise_agent_run_failure   converts a failed Agent.run into
                            ChatHealthyException with a mode naming the kind
                            of failure, the vendor, and the call site
  raise_unavailable         the same for an exhausted attempt ladder
  model_name                the model a built Agent resolved to
  embed / embedding_model_name
                            the firm's one embedding declaration

Nothing here wraps Agent.run or Agent.run_sync. Rule-065 statement 11
forbids a facade over pydantic-ai, so the agent is run by its owner and only
the failure conversion is shared.

Realizes EPIC-008-F-002-S-009-REQ-B-007. See
ChatHealthyLib/architectureAndDesign/
EPIC-003-F-003-Manage-Errors-design-v3.docx §5 for the contract.

Test-only failure injection: when the environment variable
CHATHEALTHY_INJECT_LLM_FAILURE is truthy (non-empty, non-zero), injected_failure
reports it so a call site can exercise its failure path without a real
upstream outage. Tear down by clearing the variable.
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
    """One embedding, with the firm's retry ladder around it.

    pydantic_ai.Agent is an agent abstraction and has no embeddings surface,
    so an embedding call has nothing to reach a model through. This is not a
    facade over an agent — there is no agent to wrap. It carries the same
    MAX_ATTEMPTS, the same TRANSIENT tuple, the same jittered backoff and the
    same raise_unavailable as a model call's failure path.

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
            from pydantic_ai import Embedder
            return Embedder(f"openai:{model_name_str}").embed_query_sync(
                text).embeddings[0]
        except TRANSIENT as exc:
            last_exc = exc
            if attempt < MAX_ATTEMPTS:
                time.sleep(jittered(BACKOFF_SECONDS[attempt - 1]))
                continue
    assert last_exc is not None
    raise_unavailable(provider, call_site, server, component,
                       model_name_str, last_exc)
