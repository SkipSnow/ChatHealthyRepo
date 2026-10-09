# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared source-URL discovery: the agent that answers "where is the file?".

When a source's download pointer is NOT deterministic, an agent reads the
source's index page and returns ONLY a URL -- it never fetches or parses the
data; deterministic code does that. The simple pattern: a pydantic INPUT
(DiscoveryRequest), a pydantic OUTPUT (DiscoveryAnswer), and an agent that runs
input -> output through the ChatHealthy LLM facade
(chathealthy_lib.llm.run_llm_sync over a pydantic-ai Agent) -- never a raw
vendor HTTP call. The model is read from the environment (provider:model); no
fallback, no vendor pinned in source. Retry/timeout/failure conversion are the
facade's.
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import os
import time
from typing import Callable
from urllib.parse import urljoin

import requests
from pydantic import BaseModel

_log = ChatHealthyLoggingService()

_PAGE_HTML_CHARS = 32_000  # cap the html we hand the agent
_URL_FORBIDDEN = frozenset(" \t\r\n'\"<>")


class DiscoveryRequest(BaseModel):
    """Pydantic INPUT: what the agent is asked about."""
    page_url: str
    instructions: str
    page_html: str


class DiscoveryAnswer(BaseModel):
    """Pydantic OUTPUT: where the file is. url is null when the page has no
    matching file. The agent answers only the location, never the content."""
    url: str | None = None
    version_identifier: str | None = None
    filename: str | None = None
    published_date: str | None = None


def _is_valid_url(value: str) -> bool:
    """http:// or https:// followed by at least one permitted character."""
    for scheme in ("https://", "http://"):
        if value.startswith(scheme):
            rest = value[len(scheme):]
            return bool(rest) and not any(c in _URL_FORBIDDEN for c in rest)
    return False


def _fetch_page_html(source_name: str, page_url: str, timeout_sec: int) -> str:
    try:
        resp = requests.get(page_url, timeout=timeout_sec,
                            headers={"User-Agent": "ChatHealthy-Pipeline/1.0"})
        resp.raise_for_status()
    except Exception as exc:
        raise ChatHealthyException(
            mode="source_url_discovery_page_fetch_failed",
            component="source_url_discovery",
            message=(f"source_url_discovery[{source_name}]: cannot fetch index "
                     f"page {page_url}: {exc}"),
            exception=exc) from exc
    page_html = resp.text[:_PAGE_HTML_CHARS]
    if not page_html.strip():
        raise ChatHealthyException(
            mode="source_url_discovery_page_empty",
            component="source_url_discovery",
            message=(f"source_url_discovery[{source_name}]: index page "
                     f"{page_url} returned empty body"))
    return page_html


def _discovery_model(source_name: str) -> str:
    """The provider:model string for the discovery agent, read from the env.
    No fallback and no vendor pinned in source (BUG-003)."""
    model = os.getenv("CH_URL_DISCOVERY_MODEL", "").strip()
    if not model:
        raise ChatHealthyException(
            mode="config_error",
            component="source_url_discovery",
            message=(f"source_url_discovery[{source_name}]: CH_URL_DISCOVERY_MODEL "
                     f"is not set (expected a 'provider:model' string); there is "
                     f"no fallback."))
    return model


_SYSTEM_PROMPT = (
    "You receive a JSON DiscoveryRequest with page_url, instructions, and "
    "page_html. You locate the single correct download URL for the file the "
    "instructions describe, reading only the provided page_html. You answer "
    "only WHERE the file is -- you never fetch the file and never read its "
    "contents. Return url=null when the page contains no matching file."
)


def _run_discovery_agent(source_name: str, request: DiscoveryRequest) -> DiscoveryAnswer:
    """The agent that runs: pydantic input -> pydantic output, through the
    facade. The facade owns retry, timeout and failure conversion."""
    from pydantic_ai import Agent  # noqa: PLC0415
    from chathealthy_lib.llm import run_llm_sync  # noqa: PLC0415

    model = _discovery_model(source_name)
    agent = Agent(model, output_type=DiscoveryAnswer, system_prompt=_SYSTEM_PROMPT)
    provider = model.split(":", 1)[0] if ":" in model else model
    result = run_llm_sync(
        agent, request.model_dump_json(),
        call_site=f"source_url_discovery:{source_name}",
        provider=provider, server="pipeline", component="source_url_discovery",
    )
    out = getattr(result, "output", None)
    if out is None:
        out = getattr(result, "data", None)
    if not isinstance(out, DiscoveryAnswer):
        raise ChatHealthyException(
            mode="source_url_discovery_no_output",
            component="source_url_discovery",
            message=(f"source_url_discovery[{source_name}]: agent returned no "
                     f"DiscoveryAnswer"))
    return out


def _extract_url(source_name: str, url_raw, page_url: str) -> str:
    """Validate the agent's url answer; no-match (null/blank) is fatal."""
    if url_raw is None or (isinstance(url_raw, str) and not url_raw.strip()):
        raise ChatHealthyException(
            mode="source_url_discovery_no_matching_file",
            component="source_url_discovery",
            message=f"source_url_discovery[{source_name}]: agent reported NO "
                    f"matching file on {page_url}",
            source_name=source_name, page_url=page_url)
    got_url = url_raw.strip().strip("`<>\"' \t\n")
    if not got_url.startswith("http"):
        got_url = urljoin(page_url, got_url)
    if not _is_valid_url(got_url):
        raise ChatHealthyException(
            mode="source_url_discovery_bad_url",
            component="source_url_discovery",
            message=f"source_url_discovery[{source_name}]: agent returned "
                    f"unusable URL: {got_url!r}")
    return got_url


def find_latest_source_version(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
    sleeper: Callable[[float], None] = time.sleep,  # noqa: ARG001 -- facade owns retry
) -> dict:
    """Return {url, version_identifier, filename, published_date} for the
    latest file the source page advertises, via the discovery agent."""
    page_html = _fetch_page_html(source_name, page_url, timeout_sec)
    request = DiscoveryRequest(page_url=page_url, instructions=instructions,
                               page_html=page_html)
    answer = _run_discovery_agent(source_name, request)
    got_url = _extract_url(source_name, answer.url, page_url)
    filename = (answer.filename or "").strip() or got_url.rsplit("/", 1)[-1].split("?")[0]
    version = (answer.version_identifier or "").strip() or None
    published = str(answer.published_date).strip() if answer.published_date else None
    _log.LogPipeline("INFO", "source_url_discovery[%s]: agent resolved url=%s version=%s",
                     source_name, got_url, version)
    return {"url": got_url, "version_identifier": version,
            "filename": filename, "published_date": published}


def find_latest_data_url(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """URL-only shim over find_latest_source_version for callers that only
    need the pointer."""
    return find_latest_source_version(
        source_name=source_name, page_url=page_url, instructions=instructions,
        timeout_sec=timeout_sec, sleeper=sleeper,
    )["url"]
