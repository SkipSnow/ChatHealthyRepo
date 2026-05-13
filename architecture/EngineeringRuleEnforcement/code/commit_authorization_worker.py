# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""CommitAuthorizationWorker — Rule-065 enforcement.

Implements EPIC-008-F-004-S-009-REQ-B-001: no commit/push without
explicit human authorization.

Two paths to authorization, picked by environment:

1. Real interactive shell (no agent markers, all three stdio are TTYs):
   prompt the operator inline on stderr, read from stdin.

2. Anything else (agent-driven subprocess, IDE-integrated terminal,
   piped stdio, etc.): the worker pushes a prompt to a browser by
   starting a local HTTP server on a free port and opening the user's
   default browser. The user clicks Approve or Reject in the page;
   the worker blocks until they answer or until the timeout elapses.
   This is the "push to the human" path — the agent that invoked the
   commit cannot answer because the answer comes from a separate user-
   facing browser process, not from the agent's stdin.

Wired to two git hooks via two enforcement entries on the same rule:
    Rule-065-ENF-001  pre-commit  — gates `git commit`
    Rule-065-ENF-002  pre-push    — gates `git push`
"""

from __future__ import annotations

import os
import secrets
import socket
import socketserver
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs

# Allow being run both as a script and imported as a module.
_THIS_FILE = Path(__file__).resolve()
if __package__ in (None, ""):
    sys.path.insert(0, str(_THIS_FILE.parent))
    from enforcement_worker import (
        EnforcementWorker,
        ViolationRecord,
        WorkerInternalError,
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )
else:
    from .enforcement_worker import (  # type: ignore
        EnforcementWorker,
        ViolationRecord,
        WorkerInternalError,
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )


# Env markers any of which prove the worker is running inside an agent.
_AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")

# Web-prompt timeout (seconds).
_BROWSER_TIMEOUT_SECONDS = 600


class CommitAuthorizationWorker(EnforcementWorker):
    """Rule-065: require explicit human authorization on pre-commit + pre-push."""

    SCOPE_DEFAULT = True

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0

    # ────────────────────────────────────────────────────────────────────────
    def run(self) -> int:
        action = self._action_for_hook(self.hook)

        if self._is_real_interactive_shell():
            return self._prompt_inline(action)

        return self._prompt_via_browser(action)

    # ────────────────────────────────────────────────────────────────────────
    def _is_real_interactive_shell(self) -> bool:
        """True only when no agent marker is set AND all three stdio are TTYs."""
        for marker in _AGENT_MARKERS:
            if os.environ.get(marker):
                return False
        return (
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and sys.stderr.isatty()
        )

    # ────────────────────────────────────────────────────────────────────────
    def _prompt_inline(self, action: str) -> int:
        sys.stderr.write("Approve? ")
        sys.stderr.flush()
        try:
            reply = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            self._reject(action, "interrupted")
            return EXIT_VIOLATIONS_FOUND

        if reply.strip().lower() == "approve":
            return EXIT_OK

        self._reject(action, "not approved")
        return EXIT_VIOLATIONS_FOUND

    # ────────────────────────────────────────────────────────────────────────
    def _prompt_via_browser(self, action: str) -> int:
        """Push a prompt to the user's default browser; block on their click."""
        # Bind to a free local port.
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        except Exception as exc:
            self._reject(action, f"could not allocate approval port: {exc}")
            return EXIT_VIOLATIONS_FOUND

        # Shared state between the request handler and the polling loop.
        verdict = {"value": None}  # "approve" | "reject" | None
        token = secrets.token_urlsafe(8)

        page_html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>ChatHealthy commit authorization</title>"
            "<style>body{font-family:system-ui,sans-serif;padding:40px;"
            "background:#0b7a75;color:#fff;text-align:center}"
            "h1{font-size:28px}"
            "button{font-size:18px;padding:14px 32px;margin:8px;border:none;"
            "border-radius:6px;cursor:pointer;font-weight:600}"
            ".approve{background:#0b9a94;color:#fff}"
            ".reject{background:#dc2626;color:#fff}</style></head>"
            "<body>"
            f"<h1>Authorize {action}?</h1>"
            f"<p>Token: <code>{token}</code></p>"
            f"<form method=POST action=/decide><button class=approve name=verdict value=approve>APPROVE</button>"
            f"<button class=reject name=verdict value=reject>REJECT</button>"
            f"<input type=hidden name=token value=\"{token}\"></form>"
            "</body></html>"
        )

        ack_html = (
            "<!doctype html><html><head><meta charset=utf-8></head>"
            "<body style=\"font-family:system-ui,sans-serif;padding:40px;text-align:center\">"
            "<h1>Recorded. You can close this tab.</h1></body></html>"
        )

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):  # silence access log
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
                parsed = urlparse(self.path)
                if parsed.path == "/" or parsed.path == "/prompt":
                    self._send(200, page_html)
                else:
                    self._send(404, "<h1>404</h1>")

            def do_POST(self):  # noqa: N802
                if urlparse(self.path).path != "/decide":
                    self._send(404, "<h1>404</h1>")
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length).decode("utf-8", errors="replace") if length > 0 else ""
                fields = parse_qs(raw)
                if fields.get("token", [""])[0] != token:
                    self._send(400, "<h1>Bad token</h1>")
                    return
                v = fields.get("verdict", [""])[0]
                if v in ("approve", "reject"):
                    verdict["value"] = v
                    self._send(200, ack_html)
                else:
                    self._send(400, "<h1>Bad verdict</h1>")

        try:
            server = socketserver.TCPServer(("127.0.0.1", port), _Handler, bind_and_activate=True)
        except Exception as exc:
            self._reject(action, f"could not start approval server: {exc}")
            return EXIT_VIOLATIONS_FOUND

        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()

        url = f"http://127.0.0.1:{port}/prompt"
        try:
            webbrowser.open_new(url)
        except Exception:
            # Best-effort. If the browser open fails, the user can still
            # navigate manually — but the URL is on stderr below.
            pass
        # Always also write the URL to stderr so the user can paste it if
        # the auto-open didn't land in front of them.
        sys.stderr.write(f"\nAuthorization requested at: {url}\n")
        sys.stderr.flush()

        deadline = time.time() + _BROWSER_TIMEOUT_SECONDS
        try:
            while time.time() < deadline:
                if verdict["value"] is not None:
                    break
                time.sleep(0.25)
        finally:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass

        if verdict["value"] == "approve":
            return EXIT_OK
        if verdict["value"] == "reject":
            self._reject(action, "rejected by human")
            return EXIT_VIOLATIONS_FOUND
        self._reject(action, f"approval timed out after {_BROWSER_TIMEOUT_SECONDS}s")
        return EXIT_VIOLATIONS_FOUND

    # ────────────────────────────────────────────────────────────────────────
    def _action_for_hook(self, hook: str) -> str:
        if hook == "pre-commit":
            return "commit"
        if hook == "pre-push":
            return "push"
        raise WorkerInternalError(
            f"CommitAuthorizationWorker bound to unsupported hook {hook!r}; "
            f"expected pre-commit or pre-push"
        )

    def _reject(self, action: str, reason: str) -> None:
        self._emit_violation(ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id=self.rule_id,
            resource=action,
            message=f"{action} blocked: {reason}.",
            severity="error",
        ))
        self.violation_count = 1


def main(argv: list[str] | None = None) -> int:
    return CommitAuthorizationWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
