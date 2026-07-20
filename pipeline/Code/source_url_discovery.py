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

import json
import logging
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urljoin

import requests

_log = logging.getLogger("source_url_discovery")

_AGENT_MODEL = "gemini-2.5-flash-lite"
_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_AGENT_MODEL}:generateContent"
)
_PAGE_HTML_CHARS = 32_000  # cap the html we send to the agent
_LLM_TIMEOUT_S = 60
_VALID_URL_RE = re.compile(r"^https?://[^\s'\"<>]+$")


def find_latest_data_url(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
) -> str:
    _log.info("source_url_discovery[%s]: fetching index %s", source_name, page_url)
    try:
        resp = requests.get(page_url, timeout=timeout_sec)
        resp.raise_for_status()
    except Exception as exc:
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: cannot fetch index page "
            f"{page_url}: {exc}"
        ) from exc

    page_html = resp.text[:_PAGE_HTML_CHARS]
    if not page_html.strip():
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: index page {page_url} returned "
            f"empty body"
        )

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: Gemini API key not in "
            f"env (GEMINI_API_KEY or GOOGLE_API_KEY)"
        )

    prompt = (
        f"You are reading an HTML index page from {page_url}. {instructions}\n\n"
        f"Return ONLY the absolute URL of the file. No prose, no explanation, "
        f"no markdown — just the URL. If the page does not contain a matching "
        f"file, return the single token NONE.\n\n"
        f"--- BEGIN PAGE HTML ---\n{page_html}\n--- END PAGE HTML ---"
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0},
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
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: Gemini HTTP {exc.code} {exc.reason}"
        ) from exc
    except Exception as exc:
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: Gemini call failed: {exc}"
        ) from exc

    try:
        wrapper = json.loads(raw)
        text = wrapper["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: Gemini response unparsable: "
            f"{exc} :: {raw[:400]}"
        ) from exc

    raw_text = (text or "").strip()

    if raw_text == "NONE":
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: agent reported NO matching "
            f"file on {page_url}"
        )

    raw_text = raw_text.strip("`<>\"' \t\n")
    if not raw_text.startswith("http"):
        raw_text = urljoin(page_url, raw_text)

    if not _VALID_URL_RE.match(raw_text):
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: agent returned unusable URL: "
            f"{raw_text!r}"
        )

    _log.info("source_url_discovery[%s]: agent selected %s", source_name, raw_text)
    return raw_text
