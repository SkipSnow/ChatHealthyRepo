# Copyright © 2026 Skip Snow. All rights reserved.
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

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Optional
from urllib.parse import urlparse

import requests

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
    "www.zocdoc.com",
}


class URLGuardian:
    """Validates and sanitizes URLs in LLM responses and tool results."""

    def __init__(self, cache_ttl: int = 3600, request_timeout: int = 5, max_workers: int = 4):
        self._cache: dict[str, tuple[bool, float, Optional[str]]] = {}  # url -> (valid, timestamp, redirect_url)
        self._cache_ttl = cache_ttl
        self._request_timeout = request_timeout
        self._max_workers = max_workers

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_url(self, url: str) -> tuple[bool, Optional[str]]:
        """
        Check if a URL is reachable.

        Returns:
            (is_valid, resolved_url)
            - (True, None) — URL works as-is
            - (True, new_url) — URL redirected to new_url
            - (False, None) — URL is broken and no redirect found
        """
        cached = self._cache_lookup(url)
        if cached is not None:
            return cached

        valid, redirect = self._validate(url)
        self._cache_store(url, valid, redirect)
        return valid, redirect

    def guard_tool_result(self, result: dict) -> dict:
        """
        Validate URLs in a tool result dict.

        Looks for a 'links' key containing {label: url} pairs.
        Broken URLs are removed from the dict. If a redirect is found,
        the URL is updated.

        Returns the (possibly modified) result dict.
        """
        links = result.get("links")
        if not isinstance(links, dict):
            return result

        urls_to_check = {label: url for label, url in links.items()
                         if isinstance(url, str) and url.startswith("http")}

        if not urls_to_check:
            return result

        validated = self._validate_batch(urls_to_check.values())

        for label, url in list(urls_to_check.items()):
            valid, redirect = validated.get(url, (True, None))
            if not valid:
                _log.info("URLGuardian: removing broken link %s -> %s", label, url)
                del links[label]
            elif redirect:
                _log.info("URLGuardian: redirecting %s -> %s", url, redirect)
                links[label] = redirect

        return result

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

    def _validate(self, url: str) -> tuple[bool, Optional[str]]:
        """HEAD request to check URL. Falls back to GET on 405."""
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            return False, None

        # Trust known search domains that block server-side requests
        if parsed.netloc in _TRUSTED_SEARCH_DOMAINS:
            return True, None

        try:
            resp = requests.head(url, timeout=self._request_timeout, allow_redirects=True)
            if resp.status_code == 405:
                resp = requests.get(url, timeout=self._request_timeout, allow_redirects=True, stream=True)
                resp.close()

            final_url = resp.url if resp.url != url else None
            if resp.status_code < 400:
                return True, final_url
            return False, None

        except requests.RequestException as e:
            _log.debug("URLGuardian: request failed for %s: %s", url, e)
            return False, None

    def _validate_batch(self, urls) -> dict[str, tuple[bool, Optional[str]]]:
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
            futures = {pool.submit(self._validate, url): url for url in uncached}
            for future in as_completed(futures, timeout=self._request_timeout + 2):
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
