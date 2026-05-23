# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared source-URL discovery for every external data file the pipeline
ingests.

Realizes EPIC-010-F-102-S-003-REQ-T-006: pipelines whose source URL is
auto-discovered (NPPES, NUCC, ZIP-county crosswalk, USDA RUCC, ...) MUST
identify the correct file by calling an AI agent (Claude Haiku via the
Anthropic SDK), not by regex or HTML scraping in code. No fallback URL
constants. On agent failure or unusable response, the fetcher raises and
the pipeline fails loudly.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urljoin

import requests

_log = logging.getLogger("source_url_discovery")

_AGENT_MODEL = "claude-haiku-4-5-20251001"
_PAGE_HTML_CHARS = 32_000  # cap the html we send to the agent
_VALID_URL_RE = re.compile(r"^https?://[^\s'\"<>]+$")


def find_latest_data_url(
    *,
    source_name: str,
    page_url: str,
    instructions: str,
    timeout_sec: int = 60,
) -> str:
    """Ask Claude Haiku to identify the latest data-file URL on a source page.

    Args:
        source_name: short label for logging ("nppes", "nucc", "crosswalk", "rucc").
        page_url: the source index/landing page to fetch and reason over.
        instructions: source-specific guidance — what file pattern to look for,
                      what version-stamp rules apply, what to ignore.
        timeout_sec: HTTP timeout for the index-page fetch.

    Returns:
        Absolute URL of the latest data file the agent identified.

    Raises:
        RuntimeError if the index page cannot be fetched, the agent call fails,
        or the agent returns an unusable URL. There is no fallback path.
    """
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

    api_key = os.getenv("Anthropic_API_KEY") or os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: Anthropic API key not in "
            f"env (Anthropic_API_KEY or ANTHROPIC_API_KEY)"
        )

    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    prompt = (
        f"You are reading an HTML index page from {page_url}. {instructions}\n\n"
        f"Return ONLY the absolute URL of the file. No prose, no explanation, "
        f"no markdown — just the URL. If the page does not contain a matching "
        f"file, return the single token NONE.\n\n"
        f"--- BEGIN PAGE HTML ---\n{page_html}\n--- END PAGE HTML ---"
    )

    try:
        msg = client.messages.create(
            model=_AGENT_MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: Anthropic call failed: {exc}"
        ) from exc

    raw = (msg.content[0].text if msg.content else "").strip()

    if raw == "NONE":
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: agent reported NO matching "
            f"file on {page_url}"
        )

    # The agent sometimes wraps the URL in backticks or angle brackets.
    raw = raw.strip("`<>\"' \t\n")
    if not raw.startswith("http"):
        # Possibly relative — resolve against page_url.
        raw = urljoin(page_url, raw)

    if not _VALID_URL_RE.match(raw):
        raise RuntimeError(
            f"source_url_discovery[{source_name}]: agent returned unusable URL: "
            f"{raw!r}"
        )

    _log.info("source_url_discovery[%s]: agent selected %s", source_name, raw)
    return raw
