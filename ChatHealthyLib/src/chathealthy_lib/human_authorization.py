"""A human decision, taken with a mouse, before an irreversible act.

The caller names the action and the thing it acts on; this opens a page in the
operator's browser carrying both, and blocks until they click APPROVE or
REJECT. A verdict that arrives without the mouse-click marker is refused by
the server, so a scripted POST cannot stand in for a person.

Rule-065's commit gate has served the same page since it was written. This is
that mechanism as a library function so the next thing needing a human does
not grow a second copy of it.

Returns the verdict and never raises on rejection: refusing is an answer, not
an error. The caller logs the answer and decides what it means.
"""
from __future__ import annotations

import secrets
import socket
import socketserver
import threading
import time
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

APPROVE = "approve"
REJECT = "reject"
TIMEOUT = "timeout"
UNREACHABLE = "unreachable"

_POLL_SECONDS = 0.25


@dataclass(frozen=True)
class Authorization:
    verdict: str
    human_click: bool
    subject: str
    url: str
    seconds_waited: float

    @property
    def approved(self) -> bool:
        return self.verdict == APPROVE


def _page(action: str, subject: str, token: str) -> str:
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>ChatHealthy — {action}</title>"
        "<style>body{font-family:system-ui,sans-serif;padding:40px;"
        "background:#0b7a75;color:#fff;text-align:center}"
        "h1{font-size:28px;margin-bottom:4px}"
        "p.subject{font-size:22px;font-weight:600;margin:18px 0 30px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace}"
        "button{font-size:18px;padding:14px 32px;margin:8px;border:none;"
        "border-radius:6px;cursor:pointer;font-weight:600}"
        ".approve{background:#0b9a94;color:#fff}"
        ".reject{background:#dc2626;color:#fff}</style></head><body>"
        f"<h1>Authorize {action}?</h1>"
        f"<p class=subject>{subject}</p>"
        "<button class=approve id=btn_approve type=button>APPROVE</button>"
        "<button class=reject id=btn_reject type=button>REJECT</button>"
        "<input type=hidden id=human_click value=\"false\">"
        "<script>"
        f"var TOKEN='{token}';"
        "document.addEventListener('mousedown',function(){"
        "document.getElementById('human_click').value='true';});"
        "function send(v){"
        "var hc=document.getElementById('human_click').value;"
        "fetch('/decide',{method:'POST',body:new URLSearchParams("
        "{token:TOKEN,verdict:v,human_click:hc})})"
        ".then(function(){document.body.innerHTML='<h1>Recorded.</h1>';"
        "try{window.close();}catch(e){}});}"
        "document.getElementById('btn_approve').addEventListener('click',"
        "function(){send('approve');});"
        "document.getElementById('btn_reject').addEventListener('click',"
        "function(){send('reject');});"
        "</script></body></html>"
    )


_ACK = ("<!doctype html><html><head><meta charset=utf-8></head><body "
        "style=\"font-family:system-ui,sans-serif;padding:40px;text-align:center\">"
        "<h1>Recorded. You can close this tab.</h1></body></html>")


def request_authorization(action: str, subject: str,
                          timeout_seconds: int = 600) -> Authorization:
    """Block until the operator clicks, or the timeout expires.

    `action` is the verb shown in the heading; `subject` is the thing being
    acted on and appears both on the page and on the returned record, so the
    caller's log can name exactly what was approved.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    token = secrets.token_urlsafe(8)
    decision: dict = {"verdict": None, "human_click": False}
    page_html = _page(action, subject, token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return

        def _send(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):  # noqa: N802
            if urlparse(self.path).path in ("/", "/prompt"):
                self._send(200, page_html)
            else:
                self._send(404, "<h1>404</h1>")

        def do_POST(self):  # noqa: N802
            if urlparse(self.path).path != "/decide":
                self._send(404, "<h1>404</h1>")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            fields = parse_qs(raw)
            if fields.get("token", [""])[0] != token:
                self._send(400, "<h1>Bad token</h1>")
                return
            if fields.get("human_click", [""])[0] != "true":
                self._send(400, "<h1>Rejected: a real mouse click is required.</h1>")
                return
            verdict = fields.get("verdict", [""])[0]
            if verdict not in (APPROVE, REJECT):
                self._send(400, "<h1>Bad verdict</h1>")
                return
            decision["verdict"] = verdict
            decision["human_click"] = True
            self._send(200, _ACK)

    url = f"http://127.0.0.1:{port}/prompt"
    try:
        server = socketserver.TCPServer(("127.0.0.1", port), Handler,
                                        bind_and_activate=True)
    except OSError:
        return Authorization(UNREACHABLE, False, subject, url, 0.0)

    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webbrowser.open_new(url)
    except Exception:  # noqa: BLE001
        pass

    started = time.monotonic()
    deadline = started + timeout_seconds
    while decision["verdict"] is None and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    waited = time.monotonic() - started
    server.shutdown()
    server.server_close()

    if decision["verdict"] is None:
        return Authorization(TIMEOUT, False, subject, url, waited)
    return Authorization(decision["verdict"], decision["human_click"],
                         subject, url, waited)
