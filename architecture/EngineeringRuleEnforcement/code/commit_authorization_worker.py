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

import json
import os
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timezone
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
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )
else:
    from .enforcement_worker import (  # type: ignore
        EnforcementWorker,
        ViolationRecord,
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )


# Env markers any of which prove the worker is running inside an agent.
_AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")

# Web-prompt timeout (seconds).
_BROWSER_TIMEOUT_SECONDS = 600

# EPIC-008-F-012-S-001-REQ-B-013: commits land on this branch and no other.
# qa and prod receive code only through promote_chathealthy.py.
_COMMIT_BRANCH = "dev"

# Wrong-branch notice: bounded so a refused commit never hangs on an unread page.
_NOTICE_TIMEOUT_SECONDS = 30
_NOTICE_GRACE_SECONDS = 2


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )

# Audit log: every approve/reject/timeout/interrupt/error verdict is appended
# here as one JSON line. Lives in the feature's ArchitectureDesignAndAuditDocs
# directory alongside the design docs for EPIC-008-F-002.
_AUDIT_LOG_PATH = (
    _THIS_FILE.parent.parent
    / "ArchitectureDesignAndAuditDocs"
    / "commit_authorization.log"
)


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
            authorized = self._prompt_inline(action)
        else:
            authorized = self._prompt_via_browser(action)

        if authorized != EXIT_OK:
            return authorized

        if self.hook in ("commit-msg", "pre-commit"):
            branch = self._current_branch()
            if branch != _COMMIT_BRANCH:
                return self._deny_wrong_branch(action, branch)

        return EXIT_OK

    # ────────────────────────────────────────────────────────────────────────
    def _commit_repo(self) -> str:
        """The repository git is committing in.

        The manager spawns workers with cwd=PROJECT_ROOT, so cwd cannot be
        trusted to identify the repo under commit. The manager forwards its
        own launch cwd — which git set to the committing repo — in
        CHATHEALTHY_HOOK_CWD.
        """
        return os.environ.get("CHATHEALTHY_HOOK_CWD") or os.getcwd()

    def _current_branch(self) -> str:
        try:
            out = subprocess.check_output(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                stderr=subprocess.DEVNULL,
                cwd=self._commit_repo(),
            )
        except subprocess.CalledProcessError as exc:
            raise ChatHealthyException("worker_internal", f"could not read current branch: {exc}")
        return out.decode("utf-8").strip()

    def _staged_files(self) -> list[str]:
        try:
            out = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"],
                stderr=subprocess.DEVNULL,
                cwd=self._commit_repo(),
            )
        except subprocess.CalledProcessError as exc:
            raise ChatHealthyException("worker_internal", f"could not read staged files: {exc}")
        return [line.strip() for line in out.decode("utf-8").splitlines() if line.strip()]

    # ────────────────────────────────────────────────────────────────────────
    def _deny_wrong_branch(self, action: str, branch: str) -> int:
        """REQ-B-013: refuse the commit, show the operator the violating files."""
        files = self._staged_files()
        reason = (
            f"branch is {branch!r}; commits are permitted only on "
            f"{_COMMIT_BRANCH!r}. qa and prod receive code via "
            f"promote_chathealthy.py, never via commit. "
            f"{len(files)} violating file(s)."
        )
        self._notify_wrong_branch(branch, files)
        self._reject(action, reason)
        return EXIT_VIOLATIONS_FOUND

    def _notify_wrong_branch(self, branch: str, files: list[str]) -> None:
        """Fire-and-forget browser notice naming the branch and every
        violating file. Never blocks the deny: any failure here is swallowed
        so the commit is refused regardless."""
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        except Exception:
            return

        rows = "".join(f"<li><code>{_escape(f)}</code></li>" for f in files) or \
            "<li><em>no staged files reported</em></li>"
        page_html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>ChatHealthy commit refused</title>"
            "<style>body{font-family:system-ui,sans-serif;padding:40px;"
            "background:#7f1d1d;color:#fff}"
            "h1{font-size:28px;margin-bottom:4px}"
            "ul{text-align:left;display:inline-block;margin-top:16px}"
            "code{background:rgba(0,0,0,.25);padding:2px 6px;border-radius:4px}"
            "</style></head><body>"
            "<h1>Commit refused &mdash; wrong branch</h1>"
            f"<p>You are on <code>{_escape(branch)}</code>. "
            f"Commits are permitted only on <code>{_COMMIT_BRANCH}</code>.</p>"
            "<p>qa and prod receive code only through "
            "<code>promote_chathealthy.py</code>, never through a commit.</p>"
            f"<p><strong>{len(files)} violating file(s) in this commit:</strong></p>"
            f"<ul>{rows}</ul>"
            "<p>Rule-065</p>"
            "<p>EPIC-008-F-012-S-001-REQ-B-013</p>"
            "</body></html>"
        )

        fetched = {"value": False}

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                return

            def do_GET(self):  # noqa: N802
                payload = page_html.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                fetched["value"] = True

        try:
            server = socketserver.TCPServer(
                ("127.0.0.1", port), _Handler, bind_and_activate=True
            )
        except Exception:
            return

        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/"
        try:
            webbrowser.open_new(url)
        except Exception:
            pass
        sys.stdout.write(f"\nCommit refused. Details at: {url}\n")
        sys.stdout.flush()

        deadline = time.time() + _NOTICE_TIMEOUT_SECONDS
        while time.time() < deadline and not fetched["value"]:
            time.sleep(0.1)
        if fetched["value"]:
            time.sleep(_NOTICE_GRACE_SECONDS)
            sys.stdout.write("Refusal notice DISPLAYED.\n")
        else:
            sys.stdout.write(
                f"Refusal notice NOT DISPLAYED: no browser fetched it within "
                f"{_NOTICE_TIMEOUT_SECONDS}s.\n"
            )
        sys.stdout.flush()
        try:
            server.shutdown()
            server.server_close()
        except Exception:
            pass

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
            self._audit(action, "approve", "inline")
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
            f"<button class=approve id=btn_approve type=button>APPROVE</button>"
            f"<button class=reject id=btn_reject type=button>REJECT</button>"
            f"<input type=hidden id=human_click value=\"false\">"
            "<script>"
            f"var TOKEN='{token}';"
            "document.addEventListener('mousedown',function(){document.getElementById('human_click').value='true';});"
            "function send(v){"
            "var hc=document.getElementById('human_click').value;"
            "fetch('/decide',{method:'POST',body:new URLSearchParams({token:TOKEN,verdict:v,human_click:hc})})"
            ".then(function(){document.body.innerHTML='<h1>Recorded.</h1>';try{window.close();}catch(e){}});"
            "}"
            "document.getElementById('btn_approve').addEventListener('click',function(){send('approve');});"
            "document.getElementById('btn_reject').addEventListener('click',function(){send('reject');});"
            "</script>"
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
                if fields.get("human_click", [""])[0] != "true":
                    self._send(
                        400,
                        "<h1>Rejected: human_click marker missing. A real mouse "
                        "click on APPROVE or REJECT is required.</h1>",
                    )
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
        # open_new RETURNS whether it managed to open anything, and that
        # return value used to be discarded -- so a browser that never
        # appeared was indistinguishable from one that did, and the operator
        # was left waiting on a prompt that was not on their screen until the
        # gate timed out and refused the commit. On Windows the shell's own
        # opener succeeds in contexts where webbrowser's handler does not, so
        # it is tried second, and a failure of both is said out loud.
        # The page has to arrive in front of the operator. A git hook is a
        # child of a background process, so Windows will not give it the
        # foreground and every approval landed in the tray to be hunted for.
        # The library owns that manoeuvre; this asks it rather than keeping a
        # second copy, which is how this file came to miss the fix entirely.
        raised: list[float] = []
        try:
            from chathealthy_lib.human_authorization import raise_window_to_front
            threading.Thread(
                target=raise_window_to_front,
                args=("ChatHealthy commit authorization", raised),
                daemon=True).start()
        except Exception:                           # noqa: BLE001
            pass

        opened = False
        try:
            opened = bool(webbrowser.open_new(url))
        except Exception as exc:
            sys.stdout.write(f"\nbrowser open failed: {exc}\n")
        if not opened and hasattr(os, "startfile"):
            try:
                os.startfile(url)  # noqa: S606 - the Windows shell opener
                opened = True
            except Exception as exc:
                sys.stdout.write(f"\nshell open failed: {exc}\n")
        if not opened:
            sys.stdout.write(
                "\nNO BROWSER OPENED. The approval page is only reachable at "
                "the URL below; without a click there the commit is refused "
                "when the gate times out.\n"
            )
        # Always also write the URL to stderr so the user can paste it if
        # the auto-open didn't land in front of them.
        sys.stdout.write(f"\nAuthorization requested at: {url}\n")
        sys.stdout.flush()

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
            self._audit(action, "approve", "browser")
            return EXIT_OK
        if verdict["value"] == "reject":
            self._reject(action, "rejected by human")
            return EXIT_VIOLATIONS_FOUND
        self._reject(action, f"approval timed out after {_BROWSER_TIMEOUT_SECONDS}s")
        return EXIT_VIOLATIONS_FOUND

    # ────────────────────────────────────────────────────────────────────────
    def _action_for_hook(self, hook: str) -> str:
        # commit-msg, not pre-commit: git runs commit-msg only after
        # pre-commit exits clean, so the prompt appears after every check
        # has finished and passed. Asking the operator to authorize a
        # commit that cannot proceed is the thing that ordering prevents,
        # and git provides the ordering -- the manager needs to know
        # nothing about which enforcement prompts.
        if hook in ("commit-msg", "pre-commit"):
            return "commit"
        if hook == "pre-push":
            return "push"
        raise ChatHealthyException(
            "worker_internal",
            f"CommitAuthorizationWorker bound to unsupported hook {hook!r}; "
            f"expected commit-msg, pre-commit or pre-push"
        )

    def _reject(self, action: str, reason: str) -> None:
        self._audit(action, "reject", reason)
        self._emit_violation(ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id=self.rule_id,
            resource=action,
            message=f"{action} blocked: {reason}.",
            severity="error",
        ))
        self.violation_count = 1

    def _audit(self, action: str, verdict: str, reason: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": self.hook,
            "enforcement_id": self.enforcement_id,
            "action": action,
            "verdict": verdict,
            "reason": reason,
            "pid": os.getpid(),
        }
        try:
            _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> int:
    return CommitAuthorizationWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
