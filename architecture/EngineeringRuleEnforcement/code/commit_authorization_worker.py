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

import hashlib
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
    import authorization_record
else:
    from .enforcement_worker import (  # type: ignore
        EnforcementWorker,
        ViolationRecord,
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )
    from . import authorization_record  # type: ignore

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


# Env markers any of which prove the worker is running inside an agent.
_AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")

# Web-prompt timeout (seconds).
# The budget the manager handed down; this worker holds no
# number of its own. An expiry is a rejection.
_BROWSER_TIMEOUT_SECONDS = int(os.environ.get("CHATHEALTHY_ENFORCEMENT_TIMEOUT_SECONDS") or 0)
# EPIC-008-F-012-S-001-REQ-B-013: commits land on this branch and no other.
# qa and prod receive code only through promote_chathealthy.py.
_COMMIT_BRANCH = "dev"

# Wrong-branch notice: bounded so a refused commit never hangs on an unread page.
_NOTICE_TIMEOUT_SECONDS = 30
_NOTICE_GRACE_SECONDS = 2


def _as_git_will_store(message: str) -> str:
    """The message file as it will read back off the commit.

    This hook is handed the file the operator edited, and git does not store
    it verbatim: under the default cleanup it drops comment lines, strips
    trailing whitespace, collapses runs of blank lines and trims the ends.
    Digesting the raw file would never match the commit, and post-commit
    would unmake the very commits the operator approved.
    """
    kept: list[str] = []
    for line in message.splitlines():
        if line.startswith("#"):
            continue
        line = line.rstrip()
        if not line and (not kept or not kept[-1]):
            continue
        kept.append(line)
    while kept and not kept[-1]:
        kept.pop()
    return chr(10).join(kept)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_WEB_ROOT = "Website"

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
            raise ChatHealthyException("worker_internal", f"could not read current branch: {exc}",
                exception=exc)
        return out.decode("utf-8").strip()

    def _staged_tree(self) -> str:
        """The tree the pending commit will point at, or "" if git will not say."""
        try:
            out = subprocess.run(
                ["git", "write-tree"], cwd=self._commit_repo(),
                capture_output=True, text=True, check=True)
            return out.stdout.strip()
        except Exception:  # noqa: BLE001 - absence is recorded as absence
            return ""

    def _commit_subject(self) -> str:
        """The first line of the message being approved."""
        path = os.environ.get("CHATHEALTHY_COMMIT_MSG_PATH", "")
        if not path:
            path = os.path.join(self._commit_repo(), ".git", "COMMIT_EDITMSG")
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        return line[:200]
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _staged_files(self) -> list[str]:
        try:
            out = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"],
                stderr=subprocess.DEVNULL,
                cwd=self._commit_repo(),
            )
        except subprocess.CalledProcessError as exc:
            raise ChatHealthyException("worker_internal", f"could not read staged files: {exc}",
                exception=exc)
        return [line.strip() for line in out.decode("utf-8").splitlines() if line.strip()]

    # ─── the change table: shown on the approval page, stored in the audit ───
    def _staged_name_status(self) -> list:
        """(status_word, path, extra) per staged change; git is authoritative."""
        try:
            out = subprocess.check_output(
                ["git", "diff", "--cached", "--name-status", "-M"],
                stderr=subprocess.DEVNULL, cwd=self._commit_repo())
        except subprocess.CalledProcessError as exc:
            raise ChatHealthyException(
                "worker_internal",
                f"could not read staged name-status: {exc}", exception=exc)
        words = {"A": "New", "D": "Deleting", "M": "Modified", "T": "Type-changed"}
        rows = []
        for line in out.decode("utf-8").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            code = parts[0]
            if code[:1] in ("R", "C") and len(parts) >= 3:
                rows.append(("Renamed" if code[:1] == "R" else "Copied",
                             parts[2].strip(), parts[1].strip()))
            elif len(parts) >= 2:
                rows.append((words.get(code[:1], code), parts[1].strip(), None))
        return rows

    def _backlog_names(self) -> dict:
        """Epic/feature display names and their declared sourceLocation map,
        read from the backlog once.

        sourceLocation is the tree path from the repository root that a person
        wrote on an epic and on a feature -- the business-architecture map.
        Attribution reads that declared path, not a directory's spelling.
        """
        cached = getattr(self, "_names_cache", None)
        if cached is not None:
            return cached
        epic, feature, epic_src, feat_src = {}, {}, {}, {}
        path = (Path(self._commit_repo()) / "brain" / "machine_artifacts"
                / "content" / "agile_backlog.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 - a missing backlog yields Unknown
            data = {}

        def walk(node):
            if isinstance(node, dict):
                if node.get("epic_id"):
                    if node.get("name"):
                        epic.setdefault(node["epic_id"], node["name"])
                    if node.get("sourceLocation"):
                        epic_src.setdefault(node["epic_id"], node["sourceLocation"])
                if node.get("feature_id"):
                    if node.get("name"):
                        feature.setdefault(node["feature_id"], node["name"])
                    if node.get("sourceLocation"):
                        feat_src.setdefault(node["feature_id"],
                                            node["sourceLocation"])
                for value in node.values():
                    walk(value)
            elif isinstance(node, list):
                for value in node:
                    walk(value)
        walk(data)
        self._names_cache = {"epic": epic, "feature": feature,
                             "epic_src": epic_src, "feat_src": feat_src}
        return self._names_cache

    @staticmethod
    def _containment_path(source_location: str):
        """The real tree path a sourceLocation denotes, or None for a sentinel.

        'web' is handled by the website rule in _attribute, not by containment;
        'Unknown' (cannot be mapped) and 'unimplemented' (a legitimate epic
        with no code yet) denote no code and never contain a file -- a file
        that would attribute to 'unimplemented' is a bug, not a match.
        """
        if source_location in ("web", "Unknown", "unimplemented"):
            return None
        return source_location

    def _attribute(self, relpath: str) -> tuple:
        """The (epic_id, feature_id) that owns a file.

        A file on the website is owned by the 'web' epic, and its feature is the
        top-level directory it sits in under the website tree, or 'root' at the
        website root. Every other file is owned by whichever epic or feature
        declares a sourceLocation path at, or above, where the file sits; the
        most specific such path wins. Attribution is a person's decision, never
        an agent's inference (EPIC-008-F-002-S-003-REQ-B-009): the fact is the
        sourceLocation a person wrote, or the website a person deployed. A file
        under no declared path is Unknown, a finding rather than a guess. When
        several features share the one path that contains the file, the feature
        cannot be told apart, so it is left Unknown while the epic resolves.
        """
        rel = relpath.replace("\\", "/")
        if rel == _WEB_ROOT or rel.startswith(_WEB_ROOT + "/"):
            inner = rel[len(_WEB_ROOT):].lstrip("/")
            return ("web", inner.split("/")[0] if "/" in inner else "root")
        names = self._backlog_names()

        def contains(base: str, target: str) -> bool:
            return bool(base) and (target == base
                                   or target.startswith(base.rstrip("/") + "/"))

        feat_matches = []  # (path, feature_id, epic_id)
        for fid, src in names["feat_src"].items():
            p = self._containment_path(src)
            if p and contains(p, rel):
                feat_matches.append((p, fid, fid.split("-F-")[0]))
        if feat_matches:
            longest = max(len(p) for p, _, _ in feat_matches)
            deepest = [m for m in feat_matches if len(m[0]) == longest]
            epics_seen = {eid for _, _, eid in feat_matches}
            epic_id = next(iter(epics_seen)) if len(epics_seen) == 1 else None
            feature_id = deepest[0][1] if len(deepest) == 1 else None
            if feature_id and epic_id is None:
                epic_id = deepest[0][2]
            return (epic_id, feature_id)

        epic_matches = []  # (path, epic_id)
        for eid, src in names["epic_src"].items():
            p = self._containment_path(src)
            if p and contains(p, rel):
                epic_matches.append((p, eid))
        if epic_matches:
            longest = max(len(p) for p, _ in epic_matches)
            deepest = [eid for p, eid in epic_matches if len(p) == longest]
            return (deepest[0] if len(deepest) == 1 else None, None)
        return (None, None)

    def _epic_feature_cell(self, relpath: str) -> tuple:
        epic_id, feature_id = self._attribute(relpath)
        names = self._backlog_names()
        epic = (f"{epic_id} {names['epic'].get(epic_id, '')}".strip()
                if epic_id else "Unknown Epic")
        feature = (f"{feature_id} {names['feature'].get(feature_id, '')}".strip()
                   if feature_id else "Unknown Feature")
        return epic, feature

    def _file_purpose(self, relpath: str) -> str:
        """A terse account of what the file does, read from the file itself."""
        try:
            text = (Path(self._commit_repo()) / relpath).read_text(
                encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - unreadable/deleted file
            return "Unknown"
        doc = ""
        if relpath.endswith(".py"):
            try:
                import ast  # noqa: PLC0415
                doc = ast.get_docstring(ast.parse(text)) or ""
            except Exception:  # noqa: BLE001 - a syntax error is not our concern
                doc = ""
        if not doc:
            for line in text.splitlines():
                stripped = line.strip().lstrip("#/*-!<> ").strip()
                if stripped:
                    doc = stripped
                    break
        first = doc.strip().splitlines()[0].strip() if doc.strip() else ""
        parts = first.split()
        if len(parts) > 20:
            first = " ".join(parts[:20]) + "…"
        return first or "Unknown"

    def _authored_notes(self) -> dict:
        """Per-file deltas the operator wrote in the message CHANGES block.

        One line per file under a `CHANGES:` marker: `<path> | <note>`. New and
        Deleting come from git; a modified file's delta is read here.
        """
        named = (os.environ.get("CHATHEALTHY_COMMIT_MSG_FILE", "").strip()
                 or os.environ.get("CHATHEALTHY_COMMIT_MSG_PATH", "").strip())
        if not named:
            named = os.path.join(self._commit_repo(), ".git", "COMMIT_EDITMSG")
        try:
            with open(named, encoding="utf-8") as handle:
                message = handle.read()
        except Exception:  # noqa: BLE001 - no message file, no notes
            return {}
        notes = {}
        started = False
        for raw in message.splitlines():
            line = raw.strip()
            if not started:
                if line.upper().startswith("CHANGES:"):
                    started = True
                continue
            if line.startswith("#") or " | " not in line:
                continue
            path, note = line.split(" | ", 1)
            notes[path.strip().replace("\\", "/")] = note.strip()
        return notes

    def _change_rows(self) -> list:
        """One row per staged file: full path for the audit, name for the screen."""
        cached = getattr(self, "_rows_cache", None)
        if cached is not None:
            return cached
        notes = self._authored_notes()
        rows = []
        for status, path, extra in self._staged_name_status():
            epic, feature = self._epic_feature_cell(path)
            if status == "New":
                change = "New"
            elif status == "Deleting":
                change = "Deleting"
            elif status == "Renamed":
                change = notes.get(path) or (
                    f"Renamed from {extra}" if extra else "Renamed")
            else:
                change = notes.get(path) or "Modified"
            purpose = ("Unknown" if status == "Deleting"
                       else self._file_purpose(path))
            rows.append({
                "file": path,                       # full path -> audit log
                "name": path.rsplit("/", 1)[-1],    # basename -> screen
                "status": status,
                "epic": epic,
                "feature": feature,
                "purpose": purpose,
                "change_note": change,
            })
        self._rows_cache = rows
        return rows

    def _render_change_table(self) -> str:
        body = []
        for row in self._change_rows():
            body.append(
                "<tr>"
                f"<td class=fn>{_escape(row['name'])}</td>"
                f"<td><div>{_escape(row['epic'])}</div>"
                f"<div class=sub>{_escape(row['feature'])}</div></td>"
                f"<td>{_escape(row['purpose'])}</td>"
                f"<td>{_escape(row['change_note'])}</td>"
                "</tr>")
        inner = "".join(body) or "<tr><td colspan=4>no staged files</td></tr>"
        return (
            "<div class=tablewrap><table>"
            "<colgroup><col class=c1><col class=c2><col class=c3><col class=c4>"
            "</colgroup>"
            "<thead><tr><th>File</th><th>Epic · Feature</th>"
            "<th>What it does</th><th>Change</th></tr></thead>"
            f"<tbody>{inner}</tbody></table></div>")

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
        if action == "commit":
            for row in self._change_rows():
                sys.stderr.write(
                    f"  [{row['status']}] {row['name']}\n"
                    f"      {row['epic']} / {row['feature']}\n"
                    f"      {row['purpose']} | {row['change_note']}\n")
        sys.stderr.write("Approve? ")
        sys.stderr.flush()
        try:
            reply = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            self._reject(action, "interrupted")
            return EXIT_VIOLATIONS_FOUND

        if reply.strip().lower() == "approve":
            self._audit(action, "approve", "inline")
            self._leave_evidence()
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

        if action == "commit":
            _rows = self._change_rows()
            heading = f"Authorize commit? &mdash; {len(_rows)} file(s)"
            table_html = self._render_change_table()
        else:
            heading = f"Authorize {_escape(action)}?"
            table_html = ""

        page_html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>ChatHealthy commit authorization</title>"
            "<style>body{font-family:system-ui,sans-serif;padding:24px;"
            "background:#0b7a75;color:#fff;text-align:center}"
            "h1{font-size:22px;margin:0 0 16px}"
            "button{font-size:18px;padding:14px 32px;margin:8px;border:none;"
            "border-radius:6px;cursor:pointer;font-weight:600}"
            ".approve{background:#0b9a94;color:#fff}"
            ".reject{background:#dc2626;color:#fff}"
            ".tablewrap{max-height:62vh;overflow-y:auto;overflow-x:hidden;"
            "width:880px;max-width:94vw;margin:0 auto 18px;"
            "border:1px solid #063f3c;border-radius:6px}"
            "table{border-collapse:collapse;width:100%;table-layout:fixed;"
            "font-size:12px;color:#111;background:#fff}"
            "col.c1{width:24%}col.c2{width:24%}col.c3{width:33%}col.c4{width:19%}"
            "th,td{border:1px solid #cbd5e1;padding:6px 8px;text-align:left;"
            "vertical-align:top;overflow-wrap:anywhere;word-break:break-word}"
            "th{position:sticky;top:0;background:#0b9a94;color:#fff;"
            "font-weight:600}"
            "td.fn{font-family:ui-monospace,Consolas,monospace}"
            ".sub{color:#4b5563;font-size:11px;margin-top:2px}</style></head>"
            "<body>"
            f"<h1>{heading}</h1>"
            f"{table_html}"
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
            self._leave_evidence()
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

    def _leave_evidence(self) -> None:
        """Record that this exact message was approved, for post-commit.

        `git commit --no-verify` skips pre-commit and commit-msg. It does not
        skip post-commit -- git offers no way to suppress that one -- so the
        commit still meets a hook, but only after it exists. Undoing it there
        needs an answer to "was this approved", and the answer cannot be
        "a commit-msg hook ran", because in the bypassed case none did.

        So approval leaves evidence: the digest of the message the operator
        actually saw. post-commit recomputes it from the commit and undoes
        anything that does not match. Bypassing the gate now produces a
        commit that is created and immediately unmade.

        The evidence lives under .git/, never in the tree: a marker inside
        the working tree would be a file the commit could carry.
        """
        message = self._message_being_committed()
        if message is None:
            return
        marker = Path(self._commit_repo()) / ".git" / "rule065-approved"
        marker.write_text(
            hashlib.sha256(_as_git_will_store(message).encode("utf-8"))
            .hexdigest(), encoding="utf-8")

    def _message_being_committed(self) -> str | None:
        """The message text this hook was handed.

        git passes commit-msg the path to the message file as its first
        argument, but the manager spawns workers with the enforcement id
        alone, so the path reaches here through the environment instead.
        Reading the file means the digest covers what the operator was
        actually shown, and an approval cannot be transplanted onto some
        other commit.
        """
        named = os.environ.get("CHATHEALTHY_COMMIT_MSG_FILE", "").strip()
        if named:
            path = Path(named)
            if path.is_file():
                return path.read_text(encoding="utf-8")
        return None

    def _audit(self, action: str, verdict: str, reason: str) -> None:
        """Record the verdict where the governed thing cannot reach it.

        The durable record is the Authorizations collection, told apart from a
        promotion by authorization_type. It is written best-effort and gates
        nothing: the operator has already answered, and making the ability to
        commit depend on a database being reachable would be a worse failure
        than the one it guards against.

        The file beside the design docs stays as a local trace, but it is not
        the evidence -- it lives inside the repository this rule governs, and
        the same commit can rewrite it.
        """
        now = datetime.now(timezone.utc).isoformat()
        entry = {
            "ts": now,
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
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            # It used to pass silently, so a verdict could go unrecorded with
            # nothing anywhere saying so.
            _CH_LOG.error(f"[commit] the local trace of this {verdict} could "
                          f"not be written to {_AUDIT_LOG_PATH}: {exc}")

        document = dict(entry)
        document["authorization_type"] = "commit"
        document["operator"] = os.environ.get("USERNAME", "unknown")
        document["branch"] = self._current_branch()
        # The repository, in a field that says repository. It was written to
        # commit_subject, so the one human-readable field naming what was
        # approved held a path instead, and an auditor reading the record
        # alone could not tell what had been agreed to.
        document["repository"] = self._commit_repo()
        document["commit_subject"] = self._commit_subject()
        # The commit does not exist yet -- this is commit-msg -- so there is
        # no sha to record, and matching an approval to a commit was left to
        # branch, file list and a timestamp within seconds. The tree does
        # exist: the commit about to be written points at exactly this tree,
        # so it names the approved content exactly and can be checked with
        # `git rev-parse <sha>^{tree}` long afterwards.
        document["tree"] = self._staged_tree()
        document["files"] = self._staged_files()
        # The 4-field change table the operator approved, full path and all,
        # so the audit record carries what the screen showed and more.
        if action == "commit":
            document["change_table"] = self._change_rows()
        authorization_record.append(document, tolerate_failure=True)


def main(argv: list[str] | None = None) -> int:
    return CommitAuthorizationWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
