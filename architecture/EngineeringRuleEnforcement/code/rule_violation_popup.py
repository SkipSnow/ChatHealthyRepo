# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The "Claude broke the rules" notice for Rule-003, shown with the SAME
mechanism as the git authorization gate: a page served from a local HTTP server
and opened in the browser.

It is a NOTICE, not an approval -- one OK button that closes the window. It
carries NO audit trail; its only job is to tell the operator when a write to the
backlog was attempted outside the sanctioned program, or without the elevation
the write needs. A cheat is never silent.

show() spawns this module as a DETACHED process, so the caller -- a hook worker
that must return its deny promptly, or the editor that must exit -- never blocks
on the operator's click.
"""
from __future__ import annotations

import argparse
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse

_CREATE_NO_WINDOW = 0x08000000
_VISIBLE_SECONDS = 180


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _build_notice(title: str, message: str):
    """Build (but do not start) the notice server. Returns (server, url, state);
    server is None if the port could not be bound. Factored out so a test can
    drive the page and the OK handshake without a real browser."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    state = {"ok": False}
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>{_escape(title)}</title><style>"
        "body{font-family:system-ui,sans-serif;padding:40px;background:#7f1d1d;"
        "color:#fff}h1{font-size:26px;margin:0 0 14px}"
        "p{font-size:16px;line-height:1.5;max-width:720px}"
        "button{font-size:18px;padding:14px 44px;margin-top:22px;border:none;"
        "border-radius:6px;cursor:pointer;font-weight:700;background:#fff;"
        "color:#7f1d1d}</style></head><body>"
        f"<h1>{_escape(title)}</h1><p>{_escape(message)}</p>"
        "<button id=ok type=button>OK</button><script>"
        "document.getElementById('ok').addEventListener('click',function(){"
        "fetch('/ok',{method:'POST'}).then(function(){"
        "document.body.innerHTML='<h1>Closed.</h1>';"
        "try{window.close();}catch(e){}});});"
        "</script></body></html>")

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a, **k):
            return

        def _send(self, code: int, b: str) -> None:
            raw = b.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):  # noqa: N802
            self._send(200, page) if urlparse(self.path).path == "/notice" \
                else self._send(404, "x")

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path == "/ok":
                state["ok"] = True
                self._send(200, "ok")
            else:
                self._send(404, "x")

    try:
        server = socketserver.TCPServer(("127.0.0.1", port), H,
                                        bind_and_activate=True)
    except OSError:
        return None, "", state
    return server, f"http://127.0.0.1:{port}/notice", state


def _serve(title: str, message: str) -> None:
    """Pop the notice and block until the operator clicks OK or the window times
    out. The browser is the real UX; the server behind it is what _build_notice
    builds."""
    server, url, state = _build_notice(title, message)
    if server is None:
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    sys.stderr.write(f"\n{title}: {url}\n")
    sys.stderr.flush()
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass
    deadline = time.time() + _VISIBLE_SECONDS
    while time.time() < deadline and not state["ok"]:
        time.sleep(0.25)
    server.shutdown()


def show(message: str, title: str = "Claude broke the rules") -> None:
    """Pop the notice without blocking the caller (detached child process)."""
    try:
        subprocess.Popen(
            [sys.executable, __file__, "--title", title, "--message", message],
            creationflags=_CREATE_NO_WINDOW, close_fds=True)
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--title", default="Claude broke the rules")
    parser.add_argument("--message", required=True)
    args = parser.parse_args()
    _serve(args.title, args.message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
