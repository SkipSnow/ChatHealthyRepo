# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared source-URL discovery for every external data file the pipeline
ingests.

Realizes EPIC-010-F-102-S-003-REQ-T-006: pipelines whose source URL is
auto-discovered (NPPES, NUCC, ZIP-county crosswalk, USDA RUCC, ...) MUST
identify the correct file by calling an AI agent, not by regex or HTML
scraping in code. No fallback URL constants. On agent failure or
unusable response, the fetcher raises and the pipeline fails loudly.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import json

import os
import random
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Callable, Optional
from urllib.parse import urljoin

import requests

_log = ChatHealthyLoggingService()

_AGENT_MODEL = "gemini-2.5-flash-lite"
_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_AGENT_MODEL}:generateContent"
)
_PAGE_HTML_CHARS = 32_000  # cap the html we send to the agent
_LLM_TIMEOUT_S = 60
_VALID_URL_RE = re.compile(r"^https?://[^\s'\"<>]+$")

_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = (2.0, 8.0, 32.0)
_FATAL_HTTP_CODES = (400, 401, 403)
_NO_MATCH_SENTINEL = "no_match"


class _NoMatchError(Exception):
    """Agent explicitly returned {"url": null}. Never retried."""


class _FatalDiscoveryError(Exception):
    """Deterministic failure (HTTP 4xx that isn't 429, missing key, etc.).
    Never retried; surfaced to the caller as ChatHealthyException."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class _RetryableDiscoveryError(Exception):
    """Transient failure (429, 5xx, timeout, unparsable JSON, unusable URL).
    Retried up to _MAX_ATTEMPTS times with exponential backoff + jitter."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


def _sleep_with_jitter(delay: float, sleeper: Callable[[float], None]) -> None:
    jitter = random.uniform(0.5, 1.5)
    sleeper(delay * jitter)


def _fetch_page_html(source_name: str, page_url: str, timeout_sec: int) -> str:
    try:
        resp = requests.get(page_url, timeout=timeout_sec)
        resp.raise_for_status()
    except Exception as exc:
        raise ChatHealthyException(
            mode="source_url_discovery_page_fetch_failed",
            message=(
                f"source_url_discovery[{source_name}]: cannot fetch index "
                f"page {page_url}: {exc}"
            ),
        ) from exc
    page_html = resp.text[:_PAGE_HTML_CHARS]
    if not page_html.strip():
        raise ChatHealthyException(
            mode="source_url_discovery_page_empty",
            message=(
                f"source_url_discovery[{source_name}]: index page "
                f"{page_url} returned empty body"
            ),
        )
    return page_html


def _require_api_key(source_name: str) -> str:
    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ChatHealthyException(
            mode="source_url_discovery_no_api_key",
            message=(
                f"source_url_discovery[{source_name}]: Gemini API key not "
                f"in env (GEMINI_API_KEY or GOOGLE_API_KEY)"
            ),
        )
    return api_key


def _build_prompt(page_url: str, instructions: str, page_html: str) -> str:
    return (
        f"You are reading an HTML index page from {page_url}. {instructions}\n\n"
        f"Return a strict JSON object (no prose, no markdown) with these fields:\n"
        f"  \"url\": the absolute URL of the file (required)\n"
        f"  \"version_identifier\": a short stable identifier the source "
        f"itself uses to distinguish this version from the prior one -- "
        f"e.g. the published cycle (\"2026-07\"), the year embedded in the "
        f"filename (\"2023\"), the file's basename with date component, "
        f"or a version number the page displays. This is the field a "
        f"downstream check will compare against a stored version to "
        f"decide whether to re-download.\n"
        f"  \"filename\": the URL's trailing filename\n"
        f"  \"published_date\": the file's published-date if the page "
        f"shows one, else null. ISO-8601 date string (\"YYYY-MM-DD\") if provided.\n"
        f"If the page does not contain a matching file, return "
        f"{{\"url\": null}}.\n\n"
        f"--- BEGIN PAGE HTML ---\n{page_html}\n--- END PAGE HTML ---"
    )


def _call_gemini(source_name: str, api_key: str, prompt: str) -> str:
    """Issue one Gemini call; return the raw response body text.

    Raises _RetryableDiscoveryError on 429, 5xx, or network timeout.
    Raises _FatalDiscoveryError on any deterministic 4xx other than 429.
    """
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0, "responseMimeType": "application/json"},
    }
    url = f"{_GEMINI_ENDPOINT}?key={api_key}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_S) as gresp:
            return gresp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        if exc.code == 429 or 500 <= exc.code < 600:
            raise _RetryableDiscoveryError(
                f"Gemini HTTP {exc.code} {exc.reason}"
            ) from exc
        if exc.code in _FATAL_HTTP_CODES:
            raise _FatalDiscoveryError(
                f"Gemini HTTP {exc.code} {exc.reason}"
            ) from exc
        raise _FatalDiscoveryError(
            f"Gemini HTTP {exc.code} {exc.reason}"
        ) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
        raise _RetryableDiscoveryError(f"Gemini network error: {exc}") from exc


def _parse_gemini_response(source_name: str, raw: str) -> dict:
    """Parse Gemini's outer envelope + inner JSON.

    Raises _RetryableDiscoveryError on any parse failure (retry-able:
    the LLM may have emitted malformed JSON that a retry will fix).
    Raises _NoMatchError if the agent explicitly returned {"url": null}
    (fatal - the source page structurally does not contain a match).
    Raises _RetryableDiscoveryError on an unusable URL (retryable: the
    agent may have returned a truncated string that a retry will correct).
    """
    try:
        wrapper = json.loads(raw)
        text = wrapper["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise _RetryableDiscoveryError(
            f"Gemini response envelope unparsable: {exc} :: {raw[:400]}"
        ) from exc
    raw_text = (text or "").strip().strip("`\n ")
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise _RetryableDiscoveryError(
            f"agent returned non-JSON: {exc} :: {raw_text[:400]}"
        ) from exc
    return parsed


def _extract_url(source_name: str, parsed: dict, page_url: str) -> str:
    got_url_raw = parsed.get("url")
    # Explicit no-match signal: fatal.
    if got_url_raw is None or (isinstance(got_url_raw, str) and not got_url_raw.strip()):
        raise _NoMatchError(
            f"agent reported NO matching file on {page_url}"
        )
    if not isinstance(got_url_raw, str):
        raise _RetryableDiscoveryError(
            f"agent returned non-string url: {got_url_raw!r}"
        )
    got_url = got_url_raw.strip().strip("`<>\"' \t\n")
    if not got_url.startswith("http"):
        got_url = urljoin(page_url, got_url)
    if not _VALID_URL_RE.match(got_url):
        raise _RetryableDiscoveryError(
            f"agent returned unusable URL: {got_url!r}"
        )
    return got_url


def _discover_with_retry(
    *,
    source_name: str,
    api_key: str,
    prompt: str,
    page_url: str,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    """Retry loop around Gemini call + response parse + URL validation.

    Retryable: 429, 5xx, network timeouts, unparsable JSON, unusable URL.
    Fatal (no retry): 400/401/403, agent 'no matching file'.
    After _MAX_ATTEMPTS exhausted: raises ChatHealthyException with the
    attempt count + last error text.
    """
    last_error: Optional[str] = None
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = _call_gemini(source_name, api_key, prompt)
            parsed = _parse_gemini_response(source_name, raw)
            got_url = _extract_url(source_name, parsed, page_url)
            return {
                "url": got_url,
                "raw_parsed": parsed,
            }
        except _NoMatchError as exc:
            raise ChatHealthyException(
                mode="source_url_discovery_no_matching_file",
                message=f"source_url_discovery[{source_name}]: {exc}",
                source_name=source_name,
                page_url=page_url,
            ) from exc
        except _FatalDiscoveryError as exc:
            raise ChatHealthyException(
                mode="source_url_discovery_fatal",
                message=(
                    f"source_url_discovery[{source_name}]: fatal (no retry) "
                    f"on attempt {attempt}: {exc.message}"
                ),
                source_name=source_name,
                page_url=page_url,
                attempt=attempt,
            ) from exc
        except _RetryableDiscoveryError as exc:
            last_error = exc.message
            if attempt >= _MAX_ATTEMPTS:
                break
            _sleep_with_jitter(_BACKOFF_SECONDS[attempt - 1], sleeper)
    raise ChatHealthyException(
        mode="source_url_discovery_exhausted",
        message=(
            f"source_url_discovery[{source_name}]: exhausted "
            f"{_MAX_ATTEMPTS} attempts; last error: {last_error}"
        ),
        source_name=source_name,
        page_url=page_url,
        attempts=_MAX_ATTEMPTS,
        last_error=last_error,
    )


def find_latest_source_version(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    """Return structured version facts for the latest published file the
    source page advertises.

    Structured LLM contract per Skip 2026-07-29: probes must return the
    version identifier the source itself publishes (published cycle,
    filename component with date, etc.) so source_freshness_gate can
    compare it to admin.DataSourceRegistry.version and skip download
    when the source has not changed since our last archive.

    Returns:
      {
        "url":                str  - absolute download URL
        "version_identifier": str  - short stable identifier (e.g. "2026-07",
                                     the filename basename with date, or
                                     the published-cycle string). This is
                                     the field the freshness_gate compares.
        "filename":           str  - trailing filename derived from url
        "published_date":     str|None - ISO date if the page shows one; else None
      }

    Retry contract: the Gemini call is wrapped in a bounded retry loop
    (3 attempts, 2s -> 8s -> 32s backoff + 0.5-1.5x jitter). Transient
    failures (429, 5xx, network timeout, unparsable JSON, unusable URL)
    retry; deterministic failures (400/401/403, agent explicit
    'no matching file') are fatal on first hit. `sleeper` is injectable
    so unit tests can eliminate wall-clock waits.
    """
    page_html = _fetch_page_html(source_name, page_url, timeout_sec)
    api_key = _require_api_key(source_name)
    prompt = _build_prompt(page_url, instructions, page_html)
    result = _discover_with_retry(
        source_name=source_name,
        api_key=api_key,
        prompt=prompt,
        page_url=page_url,
        sleeper=sleeper,
    )
    got_url = result["url"]
    parsed = result["raw_parsed"]
    return {
        "url": got_url,
        "version_identifier": str(parsed.get("version_identifier") or "").strip() or None,
        "filename": str(parsed.get("filename") or got_url.rsplit("/", 1)[-1].split("?")[0]).strip(),
        "published_date": (str(parsed.get("published_date")).strip() if parsed.get("published_date") else None),
    }


def find_latest_data_url(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
    sleeper: Callable[[float], None] = time.sleep,
) -> str:
    """Legacy URL-only shim over find_latest_source_version. Existing
    callers that only need the URL keep working; new callers should use
    find_latest_source_version to also get version_identifier."""
    return find_latest_source_version(
        source_name=source_name,
        page_url=page_url,
        instructions=instructions,
        timeout_sec=timeout_sec,
        sleeper=sleeper,
    )["url"]
