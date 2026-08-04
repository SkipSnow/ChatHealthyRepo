# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for source_url_discovery retry policy.

Retry contract (verified below):
  * Retry on: HTTP 429, 5xx, network timeout, unparsable JSON, unusable URL.
  * FATAL immediately on: HTTP 400/401/403, agent explicit "no matching file".
  * Bounded at 3 attempts with backoff 2s -> 8s -> 32s + 0.5-1.5x jitter.
  * Exhaustion raises ChatHealthyException(mode='source_url_discovery_exhausted').
"""

from __future__ import annotations

import json
import socket
import urllib.error

import pytest

from chathealthy_frontend_lib.exceptions import ChatHealthyException

import source_url_discovery


class _NoWaitSleeper:
    def __init__(self):
        self.calls = []

    def __call__(self, delay):
        self.calls.append(delay)


def _install_page_and_key(monkeypatch):
    monkeypatch.setattr(
        source_url_discovery,
        "_fetch_page_html",
        lambda *_a, **_kw: "<html>index page</html>",
    )
    monkeypatch.setattr(
        source_url_discovery,
        "_require_api_key",
        lambda *_a, **_kw: "fake-key",
    )


def _success_body(url: str = "https://example.com/data.csv") -> str:
    inner = json.dumps({
        "url": url,
        "version_identifier": "2026-07",
        "filename": "data.csv",
        "published_date": "2026-07-01",
    })
    outer = {"candidates": [{"content": {"parts": [{"text": inner}]}}]}
    return json.dumps(outer)


def _install_call_sequence(monkeypatch, responses):
    """responses is a list; each element is either a str (raw body to
    return) or an Exception (to raise)."""
    it = iter(list(responses))

    def _fake_call(*_a, **_kw):
        thing = next(it)
        if isinstance(thing, Exception):
            raise thing
        return thing

    monkeypatch.setattr(source_url_discovery, "_call_gemini", _fake_call)


def test_retry_on_429_succeeds_on_second_attempt(monkeypatch):
    _install_page_and_key(monkeypatch)
    http_err = urllib.error.HTTPError(
        url="https://google.example",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=None,
    )
    # First attempt: 429 (retryable). Second attempt: success.
    retryable = source_url_discovery._RetryableDiscoveryError(f"Gemini HTTP 429 {http_err.reason}")
    _install_call_sequence(monkeypatch, [retryable, _success_body()])
    sleeper = _NoWaitSleeper()
    url = source_url_discovery.find_latest_data_url(
        source_name="nucc",
        page_url="https://example.com/",
        instructions="find the csv",
        sleeper=sleeper,
    )
    assert url == "https://example.com/data.csv"
    # One backoff sleep before the successful retry.
    assert len(sleeper.calls) == 1


def test_retry_on_timeout_succeeds_on_third_attempt(monkeypatch):
    _install_page_and_key(monkeypatch)
    r_timeout = source_url_discovery._RetryableDiscoveryError("Gemini network error: timeout")
    _install_call_sequence(monkeypatch, [r_timeout, r_timeout, _success_body()])
    sleeper = _NoWaitSleeper()
    url = source_url_discovery.find_latest_data_url(
        source_name="nucc",
        page_url="https://example.com/",
        instructions="find the csv",
        sleeper=sleeper,
    )
    assert url == "https://example.com/data.csv"
    # Two backoff sleeps (between attempts 1 -> 2 and 2 -> 3).
    assert len(sleeper.calls) == 2


def test_fatal_on_401_no_retry(monkeypatch):
    _install_page_and_key(monkeypatch)
    fatal_401 = source_url_discovery._FatalDiscoveryError("Gemini HTTP 401 Unauthorized")
    _install_call_sequence(monkeypatch, [fatal_401])
    sleeper = _NoWaitSleeper()
    with pytest.raises(ChatHealthyException) as ei:
        source_url_discovery.find_latest_data_url(
            source_name="nucc",
            page_url="https://example.com/",
            instructions="find the csv",
            sleeper=sleeper,
        )
    assert ei.value.mode == "source_url_discovery_fatal"
    # No retries -> no sleep.
    assert sleeper.calls == []


def test_fatal_after_three_5xx_exhaustion(monkeypatch):
    _install_page_and_key(monkeypatch)
    r_500 = source_url_discovery._RetryableDiscoveryError("Gemini HTTP 500 Internal Server Error")
    _install_call_sequence(monkeypatch, [r_500, r_500, r_500])
    sleeper = _NoWaitSleeper()
    with pytest.raises(ChatHealthyException) as ei:
        source_url_discovery.find_latest_data_url(
            source_name="nucc",
            page_url="https://example.com/",
            instructions="find the csv",
            sleeper=sleeper,
        )
    assert ei.value.mode == "source_url_discovery_exhausted"
    # Backoffs happened between attempts, but not after the third.
    assert len(sleeper.calls) == 2
    # Configured backoffs (before jitter) are 2.0 then 8.0.
    # Jitter multiplies by [0.5, 1.5].
    assert sleeper.calls[0] >= 2.0 * 0.5 - 1e-6
    assert sleeper.calls[0] <= 2.0 * 1.5 + 1e-6
    assert sleeper.calls[1] >= 8.0 * 0.5 - 1e-6
    assert sleeper.calls[1] <= 8.0 * 1.5 + 1e-6


def test_fatal_on_no_matching_file(monkeypatch):
    _install_page_and_key(monkeypatch)
    inner = json.dumps({"url": None})
    outer = json.dumps({"candidates": [{"content": {"parts": [{"text": inner}]}}]})
    _install_call_sequence(monkeypatch, [outer])
    sleeper = _NoWaitSleeper()
    with pytest.raises(ChatHealthyException) as ei:
        source_url_discovery.find_latest_data_url(
            source_name="nucc",
            page_url="https://example.com/",
            instructions="find the csv",
            sleeper=sleeper,
        )
    assert ei.value.mode == "source_url_discovery_no_matching_file"
    # No retry - immediate fatal.
    assert sleeper.calls == []
