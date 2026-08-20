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
import os
import subprocess
import sys
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


def _page(action: str, subject: str, token: str, palette: dict, banner: str,
          detail: str) -> str:
    """One page, coloured by the caller.

    Rule-065's commit gate is teal. A page that asks about something else and
    looks identical to the commit gate invites the wrong click, so the colour
    and the banner are the caller's to set: the operator should know what they
    are approving from across the room, before reading a word.
    """
    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>ChatHealthy -- {banner}</title>"
        "<style>body{font-family:system-ui,sans-serif;padding:36px;"
        f"background:{palette['background']};color:{palette['text']};"
        "text-align:center}"
        "p.banner{font-size:13px;letter-spacing:.18em;text-transform:uppercase;"
        "font-weight:700;opacity:.85;margin:0 0 22px}"
        "h1{font-size:26px;margin:0 0 6px;font-weight:600}"
        "p.subject{font-size:21px;font-weight:700;margin:16px auto 12px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "max-width:760px;word-break:break-word}"
        "p.detail{font-size:15px;line-height:1.55;margin:0 auto 28px;"
        "max-width:640px;opacity:.92}"
        "button{font-size:18px;padding:14px 34px;margin:8px;border:none;"
        "border-radius:6px;cursor:pointer;font-weight:700}"
        f".approve{{background:{palette['approve']};color:{palette['text']}}}"
        ".reject{background:#b91c1c;color:#fff}</style></head><body>"
        f"<p class=banner>{banner}</p>"
        f"<h1>Authorize {action}?</h1>"
        f"<p class=subject>{subject}</p>"
        f"<p class=detail>{detail}</p>"
        "<button class=approve id=btn_approve type=button>APPROVE</button>"
        "<button class=reject id=btn_reject type=button>REJECT</button>"
        "<div id=why style=\"margin:10px 0;color:#a3231d;font-size:14px\"></div>"
        "<input type=hidden id=human_click value=\"false\">"
        "<script>"
        f"var TOKEN='{token}';"
        # A click on its own proves little: it can be dispatched by script, and
        # a focused button fires one from the keyboard. A pointer that travelled
        # across the page and then pressed is the evidence wanted, so both are
        # required and both must be isTrusted -- a flag the browser sets and
        # page script cannot forge.
        "var moved=0,lx=null,ly=null,pressed=false;"
        "document.addEventListener('mousemove',function(e){"
        "if(!e.isTrusted)return;"
        "if(lx!==null){moved+=Math.abs(e.clientX-lx)+Math.abs(e.clientY-ly);}"
        "lx=e.clientX;ly=e.clientY;"
        "if(moved>40&&pressed){"
        "document.getElementById('human_click').value='true';}});"
        "document.addEventListener('mousedown',function(e){"
        "if(!e.isTrusted)return;pressed=true;"
        "if(moved>40){document.getElementById('human_click').value='true';}});"
        "function send(v){"
        "var hc=document.getElementById('human_click').value;"
        "if(hc!=='true'){document.getElementById('why').textContent="
        "'Move the mouse across this window, then click.';return;}"
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


PALETTES = {
    # Rule-065's commit gate. Left as it was so the page operators already
    # know keeps looking like itself.
    "commit": {"background": "#0b7a75", "approve": "#0b9a94", "text": "#ffffff"},
    # Data migration. Deep indigo, deliberately nothing like the teal commit
    # gate: this one moves data onto the cluster serving users, and it must
    # not be mistaken at a glance for the routine one.
    "migration": {"background": "#312e81", "approve": "#4f46e5", "text": "#ffffff"},
    # Entitlement and identity changes.
    "entitlement": {"background": "#7c2d12", "approve": "#c2410c", "text": "#ffffff"},
}


def request_authorization(action: str, subject: str,
                          timeout_seconds: int = 600,
                          palette: str = "commit",
                          banner: str = "ChatHealthy authorization",
                          detail: str = "") -> Authorization:
    """Block until the operator clicks, or the timeout expires.

    `action` is the verb in the heading; `subject` is the thing being acted on
    and appears on the page and on the returned record, so the caller's log
    names exactly what was approved. `palette` and `banner` make this page
    visually distinct from every other approval page -- an operator who cannot
    tell two gates apart will eventually click the wrong one. `detail` is the
    sentence that says what happens on APPROVE, and what does not happen on
    REJECT.
    """
    colours = PALETTES.get(palette) or PALETTES["commit"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    token = secrets.token_urlsafe(8)
    decision: dict = {"verdict": None, "human_click": False}
    page_html = _page(action, subject, token, colours, banner, detail)

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

    # The page is placed deliberately: two thirds of the screen, centred, in a
    # window of its own and in front. It used to be one webbrowser call inside a
    # bare except, so when it failed -- which it does when the hook runs as a
    # child process with no session of its own -- nothing appeared, nothing said
    # so, and the run waited out its timeout for a click on a window nobody had
    # been shown.
    def _screen() -> tuple[int, int]:
        try:
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetProcessDPIAware()
            return int(user32.GetSystemMetrics(0)), int(user32.GetSystemMetrics(1))
        except Exception:                           # noqa: BLE001
            return 1920, 1080

    def _raise_the_window(title_fragment: str) -> None:
        """Bring the page to the front, and keep trying while it opens.

        Windows only lets a process set the foreground window if it already
        owns the foreground. A commit hook is a child of a background process
        and owns nothing, so the browser opens behind everything and lands in
        the tray -- which is where it has been landing. Attaching this
        thread's input queue to the current foreground thread makes the call
        legal, which is the documented way in and the reason this is not just
        SetForegroundWindow.

        Runs in a thread and polls, because the browser takes a second or two
        to create the window and there is nothing to raise before it exists.
        """
        try:
            import ctypes
            from ctypes import wintypes
        except Exception:                           # noqa: BLE001
            return
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        SW_RESTORE, SW_SHOW = 9, 5
        found: list[int] = []

        def _visit(hwnd, _lparam):
            length = user32.GetWindowTextLengthW(hwnd)
            if length:
                buffer = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buffer, length + 1)
                if title_fragment in buffer.value:
                    found.append(hwnd)
            return True

        callback = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(_visit)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            found.clear()
            user32.EnumWindows(callback, 0)
            if found:
                hwnd = found[0]
                foreground = user32.GetForegroundWindow()
                mine = kernel32.GetCurrentThreadId()
                theirs = user32.GetWindowThreadProcessId(foreground, None)
                user32.AttachThreadInput(theirs, mine, True)
                user32.ShowWindow(hwnd, SW_RESTORE)
                user32.ShowWindow(hwnd, SW_SHOW)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.AttachThreadInput(theirs, mine, False)
                if user32.GetForegroundWindow() == hwnd:
                    _raised.append(time.monotonic())
                    return
            time.sleep(0.4)

    _raised: list[float] = []
    opened = False
    if sys.platform == "win32":
        threading.Thread(
            target=_raise_the_window, args=(f"ChatHealthy -- {banner}",),
            daemon=True).start()
        sw, sh = _screen()
        w, h = int(sw * 2 / 3), int(sh * 2 / 3)
        x, y = int((sw - w) / 2), int((sh - h) / 2)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        for exe in ("msedge", "chrome"):
            try:
                subprocess.run(
                    # --app gives the page a window of its own and honours the
                    # geometry even when the browser is already running;
                    # --new-window does not, so the page landed as a tab in
                    # whatever window happened to be open.
                    ["cmd", "/c", "start", "", exe, f"--app={url}",
                     f"--window-size={w},{h}", f"--window-position={x},{y}"],
                    check=True, creationflags=flags, timeout=20)
                opened = True
                break
            except Exception:                       # noqa: BLE001
                continue
        if not opened:
            try:
                os.startfile(url)                   # noqa: S606
                opened = True
            except Exception:                       # noqa: BLE001
                pass
    if not opened:
        try:
            opened = bool(webbrowser.open_new(url))
        except Exception:                           # noqa: BLE001
            opened = False
    if not opened:
        from chathealthy_lib.logging_service import ChatHealthyLoggingService
        ChatHealthyLoggingService().error(
            "authorization page could not be opened; open %s to decide", url)

    started = time.monotonic()
    deadline = started + timeout_seconds
    while decision["verdict"] is None and time.monotonic() < deadline:
        time.sleep(_POLL_SECONDS)
    waited = time.monotonic() - started
    from chathealthy_lib.logging_service import ChatHealthyLoggingService
    ChatHealthyLoggingService().info(
        "authorization page opened=%s raised_to_front=%s waited=%.1fs",
        opened, bool(_raised), waited)
    server.shutdown()
    server.server_close()

    if decision["verdict"] is None:
        return Authorization(TIMEOUT, False, subject, url, waited)
    return Authorization(decision["verdict"], decision["human_click"],
                         subject, url, waited)
