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
import re
import urllib.error
import urllib.request
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


def find_latest_source_version(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
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
    """
    try:
        resp = requests.get(page_url, timeout=timeout_sec)
        resp.raise_for_status()
    except Exception as exc:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: cannot fetch index page "
            f"{page_url}: {exc}") from exc

    page_html = resp.text[:_PAGE_HTML_CHARS]
    if not page_html.strip():
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: index page {page_url} returned "
            f"empty body")

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: Gemini API key not in "
            f"env (GEMINI_API_KEY or GOOGLE_API_KEY)"
        )

    prompt = (
        f"You are reading an HTML index page from {page_url}. {instructions}\n\n"
        f"Return a strict JSON object (no prose, no markdown) with these fields:\n"
        f"  \"url\": the absolute URL of the file (required)\n"
        f"  \"version_identifier\": a short stable identifier the source "
        f"itself uses to distinguish this version from the prior one — "
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
            raw = gresp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: Gemini HTTP {exc.code} {exc.reason}") from exc
    except Exception as exc:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: Gemini call failed: {exc}") from exc

    try:
        wrapper = json.loads(raw)
        text = wrapper["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: Gemini response unparsable: "
            f"{exc} :: {raw[:400]}") from exc

    raw_text = (text or "").strip().strip("`\n ")
    try:
        parsed = json.loads(raw_text)
    except ValueError as exc:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: agent returned non-JSON: "
            f"{exc} :: {raw_text[:400]}") from exc

    got_url = (parsed.get("url") or "").strip() if isinstance(parsed.get("url"), str) else None
    if not got_url:
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: agent reported NO matching "
            f"file on {page_url}")
    got_url = got_url.strip("`<>\"' \t\n")
    if not got_url.startswith("http"):
        got_url = urljoin(page_url, got_url)
    if not _VALID_URL_RE.match(got_url):
        raise ChatHealthyException(mode="runtime_error", message=f"source_url_discovery[{source_name}]: agent returned unusable URL: "
            f"{got_url!r}")

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
) -> str:
    """Legacy URL-only shim over find_latest_source_version. Existing
    callers that only need the URL keep working; new callers should use
    find_latest_source_version to also get version_identifier."""
    return find_latest_source_version(
        source_name=source_name,
        page_url=page_url,
        instructions=instructions,
        timeout_sec=timeout_sec,
    )["url"]
