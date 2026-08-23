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

import datetime
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
        + _click_proof(token)
    )


def _click_proof(token: str) -> str:
    """The evidence that a person clicked, not a script.

    Shared by every gate: a page that asks a person is only worth
    anything if it can tell a person apart from a dispatched event.
    """
    return (
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


def _transfer_page(token: str, collection: str, source: dict, destination: dict,
                   authorizer: str, stamped: str, detail: str,
                   title: str = "Data Migration Authorization",
                   kind: str = "migration",
                   chip_label: str | None = None) -> str:
    """The page that authorizes a data transfer.

    It answers the question the operator actually has, which is not "authorize
    this?" but "what moves, from where, to where". The collection is shown on
    both sides because arriving under a different name would be a different
    act; the cluster and the database are named on both sides because those
    are what make one side the pipeline and the other the one serving users.
    """
    head_shade = _CHROME["header"].get(kind, "#e9a3e0")
    panel_shade = _CHROME["panel"].get(kind, "#f6e2f5")
    body_shade = _CHROME["body"].get(kind, "#6a2b9d")
    approve_shade = _CHROME["approve"].get(kind, "#3b46e0")

    def panel(side: str, facts: dict) -> str:
        # Whatever the caller named. A migration names a cluster and a
        # database; a promotion names an environment and a branch. The page
        # renders the question it was handed rather than one shape of it.
        rows = "".join(
            f"<div class=row><span class=k>{key}:</span>"
            f"<span class=v>{value}</span></div>"
            for key, value in facts.items())
        return (
            "<div class=col>"
            f"<div class=side>{side}</div>"
            "<div class=panel>"
            f"<div class=head>{rows}</div>"
            + ("" if chip_label else
               f"<div class=body><span class=chip>{collection}</span></div>")
            + "</div></div>")

    return (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>ChatHealthy -- {title}</title>"
        "<style>"
        "*{box-sizing:border-box}"
        f"body{{margin:0;font-family:system-ui,sans-serif;background:{body_shade};"
        "color:#fff;min-height:100vh}"
        f"header{{background:{head_shade};color:#000;padding:18px 24px 14px;"
        "text-align:center;border-bottom:3px solid #000}"
        "header h1{margin:0;font-size:38px;font-weight:500;line-height:1.15}"
        "header .when{margin-top:10px;font-size:22px}"
        "header .when b{font-weight:600}"
        "header .when small{font-size:15px}"
        "main{padding:34px 28px 10px}"
        ".grid{display:flex;align-items:center;justify-content:center;gap:22px;"
        "flex-wrap:wrap;max-width:1180px;margin:0 auto}"
        ".col{flex:1 1 380px;min-width:300px}"
        ".side{font-size:30px;font-style:italic;text-align:center;"
        "margin-bottom:14px}"
        f".panel{{border:2px solid #000;background:{panel_shade}}}"
        f".head{{background:{head_shade};border-bottom:2px solid #000;padding:10px 14px}}"
        ".row{display:flex;gap:14px;font-size:19px;color:#000;padding:2px 0}"
        ".k{flex:0 0 110px}"
        ".v{font-weight:500;word-break:break-word}"
        ".body{padding:26px 14px;min-height:170px;display:flex;"
        "align-items:center;justify-content:center}"
        ".chip{background:#1f1f5c;color:#fff;padding:5px 10px;font-size:19px;"
        "font-family:ui-monospace,SFMono-Regular,Menlo,monospace;"
        "word-break:break-all}"
        ".arrow{flex:0 0 auto;color:#1a6f7f;font-size:60px;line-height:1}"
        ".named{max-width:1180px;margin:26px auto 0;text-align:center}"
        ".nk{font-size:17px;letter-spacing:.10em;text-transform:uppercase;opacity:.85;margin-bottom:8px}"
        "p.detail{max-width:900px;margin:26px auto 0;font-size:15px;"
        "line-height:1.55;text-align:center;opacity:.93}"
        ".buttons{text-align:center;padding:22px 0 40px}"
        "button{font-size:26px;font-weight:700;letter-spacing:.04em;"
        "padding:14px 52px;margin:0 26px;color:#fff;cursor:pointer;"
        "border:4px solid #1b1b6b;border-radius:8px}"
        ".reject{background:#c0121a}"
        f".approve{{background:{approve_shade}}}"
        "#why{text-align:center;color:#ffd7d5;font-size:15px;min-height:20px}"
        "</style></head><body>"
        "<header>"
        f"<h1>{title}{authorizer}</h1>"
        f"<div class=when>{stamped}</div>"
        "</header><main><div class=grid>"
        + panel("From", source)
        + "<div class=arrow>&#10142;</div>"
        + panel("To", destination)
        + "</div>"
        # Named and shown once. A migration's collection appears on both
        # sides because arriving under a different name would be a different
        # act; a commit message does not change in transit, so printing it
        # twice says nothing and labels it as nothing.
        + (f"<div class=named><div class=nk>{chip_label}</div>"
           f"<span class=chip>{collection}</span></div>" if chip_label else "")
        + f"<p class=detail>{detail}</p></main>"
        "<div class=buttons>"
        "<button class=reject id=btn_reject type=button>REJECT</button>"
        "<button class=approve id=btn_approve type=button>APPROVE</button>"
        "</div><div id=why></div>"
        + _click_proof(token)
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
    # Promotion. Advancing a baseline one environment forward is neither a
    # commit nor a data migration, and it had been borrowing the migration
    # page: the operator was asked to approve a data migration while the
    # record written said promotion. Green, which none of the others use.
    "promote": {"background": "#14532d", "header": "#86efac", "panel": "#dcfce7",
                "approve": "#15803d", "text": "#ffffff"},
}

# The transfer page's own chrome, per palette. Absent means the migration
# colours, so that page renders exactly as it did.
_CHROME = {
    "header": {"migration": "#e9a3e0", "promote": "#86efac",
               "commit": "#7fd6d1", "entitlement": "#fdba74"},
    "panel": {"migration": "#f6e2f5", "promote": "#dcfce7",
              "commit": "#e2f7f5", "entitlement": "#ffedd5"},
    "body": {"migration": "#6a2b9d", "promote": "#14532d",
             "commit": "#0b7a75", "entitlement": "#7c2d12"},
    "approve": {"migration": "#3b46e0", "promote": "#15803d",
                "commit": "#0b9a94", "entitlement": "#c2410c"},
}


def raise_window_to_front(title_fragment: str,
                          raised: list | None = None) -> None:
    """Bring a browser window to the front, and keep trying while it opens.

    Windows only lets a process set the foreground window if it already owns
    the foreground. A git hook is a child of a background process and owns
    nothing, so the page opens behind everything and lands in the tray --
    which is where every approval landed until this existed. Attaching this
    thread's input queue to the foreground thread makes the call legal, which
    is the documented way in and the reason this is not just
    SetForegroundWindow.

    Polls, because the browser takes a second or two to create the window and
    there is nothing to raise before it exists. Appends to `raised` when the
    OS actually granted the foreground, so a caller can report what happened
    rather than assume it.
    """
    raised = [] if raised is None else raised
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:                               # noqa: BLE001
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
                raised.append(time.monotonic())
                return
        time.sleep(0.4)


def request_authorization(action: str, subject: str,
                          timeout_seconds: int = 600,
                          palette: str = "commit",
                          banner: str = "ChatHealthy authorization",
                          detail: str = "",
                          transfer: dict | None = None) -> Authorization:
    """Block until the operator clicks, or the timeout expires.

    `action` is the verb in the heading; `subject` is the thing being acted on
    and appears on the page and on the returned record, so the caller's log
    names exactly what was approved. `palette` and `banner` make this page
    visually distinct from every other approval page -- an operator who cannot
    tell two gates apart will eventually click the wrong one. `detail` is the
    sentence that says what happens on APPROVE, and what does not happen on
    REJECT.

    `transfer` asks the transfer question instead: given
    {collection, source: {cluster, database}, destination: {cluster, database},
    authorizer}, the page names what moves and the two ends it moves between,
    which is what an operator releasing data needs to see. The click evidence,
    the token and the timeout are the same on both pages.
    """
    colours = PALETTES.get(palette) or PALETTES["commit"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    token = secrets.token_urlsafe(8)
    decision: dict = {"verdict": None, "human_click": False}
    if transfer:
        by = transfer.get("authorizer") or ""
        now = datetime.datetime.now().astimezone()
        stamped = (f"<b>{now.strftime('%I').lstrip('0')}:{now.strftime('%M')}</b> "
                   f"<small>{now.strftime('%Z')}</small>"
                   "&nbsp;&nbsp;&nbsp;&nbsp;"
                   f"<b>{now.month}/{now.day}/{now.year}</b>")
        page_html = _transfer_page(
            token, transfer["collection"], transfer["source"],
            transfer["destination"], f" by: {by}" if by else "", stamped, detail,
            title=banner or "Data Migration Authorization", kind=palette,
            chip_label=transfer.get("chip_label"))
    else:
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

    _raised: list[float] = []
    opened = False
    if sys.platform == "win32":
        threading.Thread(
            target=raise_window_to_front,
            args=(f"ChatHealthy -- {banner}", _raised),
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
