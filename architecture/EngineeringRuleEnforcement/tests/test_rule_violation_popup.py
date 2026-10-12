# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The notice popup is exercised for real: the test acts as the browser --
fetching the served page and clicking OK through the real endpoint -- and
asserts the handshake. The only thing it cannot assert is that the OS renders a
window; that is the operator's eyes (confirmed: the window appears and OK closes
it)."""
import sys
import threading
import time
import urllib.request
from pathlib import Path

CODE = Path(__file__).resolve().parents[1] / "code"
sys.path.insert(0, str(CODE))

import rule_violation_popup as rvp  # noqa: E402


def _get(url):
    return urllib.request.urlopen(url, timeout=5).read().decode("utf-8")


def _post(url):
    req = urllib.request.Request(url, method="POST", data=b"")
    return urllib.request.urlopen(req, timeout=5).read()


def test_notice_serves_the_content_and_ok_closes_it():
    server, url, state = rvp._build_notice("RULE VIOLATION TITLE",
                                           "the message body shown to you")
    assert server is not None
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        page = _get(url)
        assert "RULE VIOLATION TITLE" in page
        assert "the message body shown to you" in page
        assert "OK" in page and "/ok" in page
        assert state["ok"] is False

        _post(url.replace("/notice", "/ok"))          # click OK
        for _ in range(40):
            if state["ok"]:
                break
            time.sleep(0.05)
        assert state["ok"] is True, "clicking OK must close the notice"
    finally:
        server.shutdown()


def test_serve_opens_the_browser_and_returns_on_ok(monkeypatch):
    opened = []

    def fake_open(url):               # stand in for the real browser + OK click
        opened.append(url)
        _get(url)
        _post(url.replace("/notice", "/ok"))
        return True

    monkeypatch.setattr(rvp.webbrowser, "open", fake_open)
    monkeypatch.setattr(rvp, "_VISIBLE_SECONDS", 5)
    started = time.time()
    rvp._serve("T", "M")
    assert opened and opened[0].endswith("/notice"), "the browser must be opened"
    assert time.time() - started < 5, "it must return on OK, not on timeout"
