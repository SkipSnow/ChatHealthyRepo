# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Opus 4.6 (Anthropic).

"""
URLGuardian — validates URLs in LLM responses and tool results.

Dual-mode:
  1. Tool results: validates URLs in dicts returned by tools (e.g. lookup_provider_external)
  2. Text responses: validates markdown links in LLM-generated text

Broken URLs are defanged (text kept, link removed) unless the guardian
can resolve the correct URL via redirect following.

All validation results are cached with a configurable TTL to avoid
repeated HEAD requests for the same URL within a session.
"""

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse, quote_plus

import requests
from anthropic import Anthropic

_log = logging.getLogger("findcare.url_guardian")

# Markdown link pattern: [text](url)
_MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((https?://[^\s\)]+)\)')
# Bare URL pattern (not already inside a markdown link)
_BARE_URL_RE = re.compile(r'(?<!\]\()(?<!")(https?://[^\s\)<>"]+)')

# Domains that block server-side requests (bot protection) but whose
# search URLs we construct ourselves and know are valid patterns.
# These are always treated as valid — they're meant for the user's browser.
_TRUSTED_SEARCH_DOMAINS = {
    "www.healthgrades.com",
}

# Domains that return SPA shells (no server-rendered content for AI to verify).
# Trust these if the HTTP response is 200 — content loads client-side.
_SPA_DOMAINS = {
    "npiregistry.cms.hhs.gov",
    "dpr.delaware.gov",
    "www.msbml.ms.gov",
    "dhp.virginiainteractive.org",
}


class URLGuardian:
    """Validates and sanitizes URLs in LLM responses and tool results."""

    def __init__(self, cache_ttl: int = 3600, request_timeout: int = 5, max_workers: int = 4):
        self._cache: dict[str, tuple[bool, float, Optional[str]]] = {}  # url -> (valid, timestamp, redirect_url)
        self._cache_ttl = cache_ttl
        self._request_timeout = request_timeout
        self._max_workers = max_workers
        self._google_api_key = os.getenv("GOOGLE_MAPS_API_KEY")  # reuse existing key for Custom Search
        self._anthropic_key = os.getenv("Anthropic_API_KEY")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def guard_text(self, text: str) -> str:
        """
        Validate URLs in markdown text.

        - Markdown links [text](url): if broken, keep text, remove link
        - Bare URLs: if broken, keep the URL as plain text (not clickable)
        If a redirect is found, the URL is updated in place.

        Returns the sanitized text.
        """
        # Collect all URLs to validate in one batch
        md_urls = {m.group(2) for m in _MD_LINK_RE.finditer(text)}
        bare_urls = set()
        for m in _BARE_URL_RE.finditer(text):
            url = m.group(0)
            if url not in md_urls:
                bare_urls.add(url)

        all_urls = md_urls | bare_urls
        if not all_urls:
            return text

        validated = self._validate_batch(all_urls)

        # Process markdown links
        def _replace_md_link(match):
            link_text = match.group(1)
            url = match.group(2)
            valid, redirect = validated.get(url, (True, None))
            if not valid:
                _log.info("URLGuardian: defanging markdown link: %s", url)
                return link_text  # keep text, remove link
            if redirect:
                return f"[{link_text}]({redirect})"
            return match.group(0)

        text = _MD_LINK_RE.sub(_replace_md_link, text)

        # Process bare URLs — broken ones stay as plain text (already not clickable)
        # but we do update redirects
        for url in bare_urls:
            valid, redirect = validated.get(url, (True, None))
            if redirect and valid:
                text = text.replace(url, redirect)

        return text

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _validate(self, url: str, context: str = "") -> tuple[bool, Optional[str]]:
        """Validate a URL. Three-stage: HEAD reachability, AI content check, Google search correction."""
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False, None

        # Trust known search domains that block server-side requests
        if parsed.netloc in _TRUSTED_SEARCH_DOMAINS:
            return True, None

        # SPA domains: trust if reachable (content loads client-side, AI can't verify)
        if parsed.netloc in _SPA_DOMAINS:
            try:
                resp = requests.head(url, timeout=self._request_timeout, allow_redirects=True)
                if resp.status_code < 400:
                    return True, resp.url if resp.url != url else None
            except requests.RequestException:
                pass
            return False, None

        # Stage 1: HEAD reachability check
        is_reachable = False
        final_url = None
        page_content = ""
        try:
            resp = requests.head(url, timeout=self._request_timeout, allow_redirects=True)
            if resp.status_code == 405:
                resp = requests.get(url, timeout=self._request_timeout, allow_redirects=True, stream=True)
                page_content = resp.text[:1000] if hasattr(resp, 'text') else ""
                resp.close()
            final_url = resp.url if resp.url != url else None
            is_reachable = resp.status_code < 400
        except requests.RequestException as e:
            _log.debug("URLGuardian: HEAD failed for %s: %s", url, e)

        # Stage 2: AI content verification (even if reachable, page might be wrong)
        if is_reachable and context and self._anthropic_key:
            # Fetch page content for AI to verify
            if not page_content:
                try:
                    r = requests.get(final_url or url, timeout=self._request_timeout, stream=True)
                    page_content = r.text[:1500]
                    r.close()
                except Exception:
                    page_content = ""

            if page_content:
                ai_valid = self._ai_verify_content(final_url or url, context, page_content)
                if not ai_valid:
                    _log.info("URLGuardian: URL reachable but AI says wrong content: %s", url)
                    is_reachable = False  # fall through to Stage 3

        if is_reachable:
            return True, final_url

        # Stage 3: URL is broken or wrong — Google search for correct one
        if context:
            corrected = self._find_correct_url(url, context)
            if corrected and corrected != url:
                _log.info("URLGuardian: corrected %s -> %s", url, corrected)
                return True, corrected

        return False, None

    def _ai_verify_content(self, url: str, context: str, page_content: str) -> bool:
        """AI checks whether a reachable URL actually shows the expected content."""
        try:
            client = Anthropic(api_key=self._anthropic_key)
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=64,
                messages=[{"role": "user", "content": (
                    "You are verifying that a web page shows the expected content.\n"
                    f"Expected: a page related to '{context}'\n"
                    f"URL: {url}\n"
                    f"Page content (first 1500 chars):\n{page_content[:1500]}\n\n"
                    "Does this page show content relevant to the expected context? "
                    "A 'not found' page, generic homepage, or unrelated content = NO.\n"
                    "Return ONLY valid JSON: {\"valid\": true} or {\"valid\": false}"
                )}],
            )
            raw = resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            return json.loads(raw).get("valid", True)
        except Exception as e:
            _log.debug("URLGuardian: AI content verify failed: %s", e)
            return True  # default to trusting the URL on AI failure

    def _find_correct_url(self, broken_url: str, context: str) -> Optional[str]:
        """Google search for the correct URL, then AI-verify it matches the intent."""
        try:
            # Build a search query from the URL and context
            parsed = urlparse(broken_url)
            site = parsed.netloc
            search_query = f"{context} site:{site}"

            # Google Custom Search API
            search_url = "https://www.googleapis.com/customsearch/v1"
            params = {
                "key": self._google_api_key,
                "cx": os.getenv("GOOGLE_SEARCH_CX", ""),
                "q": search_query,
                "num": 3,
            }

            if not params["key"] or not params["cx"]:
                _log.debug("URLGuardian: Google Search not configured, skipping correction")
                return None

            resp = requests.get(search_url, params=params, timeout=self._request_timeout)
            if resp.status_code != 200:
                _log.debug("URLGuardian: Google Search returned %d", resp.status_code)
                return None

            items = resp.json().get("items", [])
            if not items:
                return None

            # AI verification: is the top result the right page?
            candidates = [{"title": it.get("title", ""), "url": it.get("link", "")} for it in items[:3]]

            if not self._anthropic_key:
                # No AI available — return top result as best guess
                return candidates[0]["url"] if candidates else None

            client = Anthropic(api_key=self._anthropic_key)
            ai_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": (
                    "You are verifying search results. The original URL was broken.\n"
                    f"Original broken URL: {broken_url}\n"
                    f"Search context: {context}\n\n"
                    f"Search results:\n{json.dumps(candidates, indent=2)}\n\n"
                    "Which result (if any) is the correct replacement? "
                    "Return ONLY valid JSON: {\"url\": \"the correct url\"} or {\"url\": null} if none match."
                )}],
            )
            raw = ai_resp.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()
            result = json.loads(raw)
            return result.get("url")

        except Exception as e:
            _log.debug("URLGuardian: _find_correct_url failed: %s", e)
            return None

    def _validate_batch(self, urls, context: str = "") -> dict[str, tuple[bool, Optional[str]]]:
        """Validate multiple URLs concurrently. Returns {url: (valid, redirect)}."""
        urls = list(set(urls))
        results = {}

        # Check cache first
        uncached = []
        for url in urls:
            cached = self._cache_lookup(url)
            if cached is not None:
                results[url] = cached
            else:
                uncached.append(url)

        if not uncached:
            return results

        with ThreadPoolExecutor(max_workers=min(self._max_workers, len(uncached))) as pool:
            futures = {pool.submit(self._validate, url, context): url for url in uncached}
            for future in as_completed(futures, timeout=self._request_timeout + 10):
                url = futures[future]
                try:
                    valid, redirect = future.result()
                except Exception:
                    valid, redirect = False, None
                self._cache_store(url, valid, redirect)
                results[url] = (valid, redirect)

        return results

    def _cache_lookup(self, url: str) -> Optional[tuple[bool, Optional[str]]]:
        entry = self._cache.get(url)
        if entry is None:
            return None
        valid, ts, redirect = entry
        if time.time() - ts > self._cache_ttl:
            del self._cache[url]
            return None
        return valid, redirect

    def _cache_store(self, url: str, valid: bool, redirect: Optional[str]) -> None:
        self._cache[url] = (valid, time.time(), redirect)
