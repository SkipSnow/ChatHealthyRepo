# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""EPIC-008-F-002-S-013-REQ-B-001 — a human approves every Claude-issued
command that changes the state of a ChatHealthy git repository.

Fires on PreToolUse, before the command runs. The tool payload arrives on
stdin: the manager never reads its own, so the worker inherits it.

WHY THIS EXISTS. On 2026-09-10 Claude rewrote all 2697 commits with
git filter-repo and force-pushed two remote branches. Nothing asked. The
commit gate hangs off the commit-msg hook, and a rewrite authors no commit,
so the hook could not fire; the pre-push hook is installed and carries no
enforcement. The only thing that refused was GitHub's own branch protection
on dev -- a control outside this repository. This worker is the gate that
was missing.

NO DOUBLE GOVERNANCE. The requirement allows a promote local->dev two gates
(its own, where the note may be edited, and the commit gate) and every other
remote change exactly one. So a command that already meets a gate is passed
through here: `git commit` meets the commit-msg gate, and the three chain
surfaces carry their own. Gating them again would make two prompts for one
act, which is what the requirement forbids.

EVIDENCE OF A HUMAN. The approval page records mousemove as well as the
click on the button. A click can be synthesised by script in the page; a
trail of movement arriving before it is what distinguishes a person at the
machine from anything that merely posts the form.
"""
from __future__ import annotations

import json
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
from urllib.parse import parse_qs, urlparse

_HERE = Path(__file__).resolve()
sys.path.insert(0, str(_HERE.parent))
for _candidate in _HERE.parents:
    _lib = _candidate / "ChatHealthyLib" / "src"
    if _lib.is_dir():
        sys.path.insert(0, str(_lib))
        break

from chathealthy_lib import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

import authorization_record  # noqa: E402

log = ChatHealthyLoggingService()

COMPONENT = "git_state_change_authorization_worker"
REQUIREMENT = "EPIC-008-F-002-S-013-REQ-B-001"

# A refusal travels as JSON on stdout, and the process still exits 0.
#
# Exit 2 is the other documented way to block, and it cannot be used here:
# EnforcementWorker defines EXIT_WORKER_ERROR = 2, so the manager reads a 2
# as "this worker crashed" and promotes it to 5 -- and 5 does not block.
# Measured on 2026-09-11: the operator rejected, the worker exited 2, the
# manager exited 5, the git command ran. The exit code is the manager's
# vocabulary; the decision is the harness's.
EXIT_ALLOW = 0
EXIT_AFTER_DENY = 0

APPROVAL_TIMEOUT_SECONDS = 600

# Every git subcommand that writes something: the index, the working tree,
# a ref, an object, or a remote. Listed rather than detected so the set is
# reviewable data. A subcommand absent from here changes nothing and is not
# gated -- status, log, diff, show, rev-parse, ls-files and the rest.
MUTATING_SUBCOMMANDS = frozenset({
    "add", "am", "apply", "branch", "cherry-pick", "checkout", "clean",
    "commit", "filter-branch", "filter-repo", "gc", "merge", "mv",
    "notes", "prune", "pull", "push", "rebase", "reflog", "remote", "repack",
    "replace", "reset", "restore", "revert", "rm", "stash", "submodule",
    "switch", "symbolic-ref", "tag", "update-ref", "worktree",
})

# fetch is deliberately absent. It writes only remote-tracking refs -- this
# machine's note of what the remote holds. It alters no branch, no working
# tree and nothing on the remote, and every promote must read origin/<branch>
# to establish its baseline, so gating fetch stops the chain surface that
# carries its own two gates. `pull` stays gated: it fetches AND merges into
# the branch you are on, which is a real change to local state.

# Commands that already meet a governance gate. Passing them through is the
# requirement's own instruction, not a convenience.
#
# `add` is here because staging is part of making a commit, and the commit
# is what the commit-msg gate authorises -- it shows the operator the files
# before recording them. Gating the staging too made two gates for one act,
# which is the double governance the requirement forbids. The index is also
# not a governed outcome: nothing is recorded until the commit, and a
# staged file can be unstaged. Measured 2026-09-11: the operator asked for
# a normal commit and got the tool gate on the `git add` first.
ALREADY_GATED_SUBCOMMANDS = frozenset({"add", "commit"})
ALREADY_GATED_SURFACES = (
    "promote_chathealthy.py",
    "build_chathealthy.py",
    "deploy_chathealthy.py",
)

# A rewrite or a remote write cannot be undone from this workstation. Named
# so the operator sees which kind of act they are being asked to authorise.
IRREVERSIBLE = frozenset({
    "filter-repo", "filter-branch", "push", "reset", "gc", "prune",
    "reflog", "update-ref", "replace",
})

SEPARATORS = ("&&", "||", ";", "|", "\n")


def _hook_payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def _without_heredoc_bodies(command: str) -> str:
    """The command with every heredoc body removed.

    A heredoc body is data handed to a program's stdin, not a line this
    shell runs. Reading it as commands made the gate fire on any script
    whose source merely mentions git -- including this worker's own tests,
    which put three pages in front of the operator for commands that were
    never going to run. The `<<` line itself is kept, because that IS a
    command; only the bytes between it and its delimiter are dropped.
    """
    lines = command.split("\n")
    kept: list[str] = []
    delimiter = ""
    for line in lines:
        if delimiter:
            if line.strip() == delimiter:
                delimiter = ""
            continue
        kept.append(line)
        if "<<" not in line:
            continue
        tail = line.split("<<", 1)[1].lstrip()
        if tail.startswith("<"):
            continue                        # << < is a redirect, not a heredoc
        if tail.startswith("-"):
            tail = tail[1:]                 # <<- strips leading tabs
        word = tail.split()[0] if tail.split() else ""
        delimiter = word.strip("'\"")
    return "\n".join(kept)


def _segments(command: str) -> list[str]:
    """The command split at shell separators, so a git call hidden behind
    an && is examined rather than missed."""
    parts = [_without_heredoc_bodies(command)]
    for sep in SEPARATORS:
        nxt: list[str] = []
        for part in parts:
            nxt.extend(part.split(sep))
        parts = nxt
    return [p.strip() for p in parts if p.strip()]


def _git_subcommand(segment: str) -> str:
    """The subcommand of a git invocation, or "" when the segment is not one.

    Handles `git`, `git.exe`, a full path to git, `python -m git_filter_repo`,
    and leading environment assignments.
    """
    words = segment.split()
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]                      # FOO=bar git push
    if not words:
        return ""

    head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head in ("python", "python.exe", "python3", "py"):
        if "git_filter_repo" in segment or "git-filter-repo" in segment:
            return "filter-repo"
        return ""
    if head not in ("git", "git.exe"):
        return ""

    for word in words[1:]:
        if word.startswith("-"):
            continue                            # git -C path push
        if word in ("-C", "-c"):
            continue
        return word.lower()
    return ""


def _invoked_program(segment: str) -> str:
    """The program this segment actually runs, bare of its directory.

    A chain surface is recognised by what is invoked, never by the text
    appearing somewhere in the line. Matching the text let a force-push
    through if the words promote_chathealthy.py showed up anywhere in it --
    in a comment, in a flag value, in an echo -- which turned the
    no-double-governance accommodation into a way past the gate.
    """
    words = segment.split()
    while words and "=" in words[0] and not words[0].startswith("-"):
        words = words[1:]
    if not words:
        return ""
    head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
    if head in ("python", "python.exe", "python3", "py"):
        for word in words[1:]:
            if word.startswith("-"):
                continue
            return word.replace("\\", "/").rsplit("/", 1)[-1].lower()
        return ""
    return head


# Several subcommands both read and write depending on their flags.
# `git tag` creates a tag and `git tag -l` lists them; `git remote add`
# writes and `git remote -v` reads. Listing forms were gating, which put a
# page in front of the operator for a command that changes nothing -- and a
# gate that fires on reads teaches its own operator to click through.
READ_ONLY_FLAGS = frozenset({
    "-l", "--list", "-v", "--verbose", "-n", "--dry-run", "--get",
    "--get-all", "--get-regexp", "--show-current", "--points-at",
    "--contains", "--merged", "--no-merged",
})
READ_ONLY_WORDS = frozenset({"list", "show", "get", "get-url"})


def _is_read_only_form(segment: str, subcommand: str) -> bool:
    words = segment.split()
    try:
        after = words[words.index(subcommand) + 1:]
    except ValueError:
        return False
    for word in after:
        if word in READ_ONLY_FLAGS or word in READ_ONLY_WORDS:
            return True
    return False


ANCESTRY_DEPTH_LIMIT = 24


def _ancestor_chain_surface() -> str:
    """The chain surface that this process descends from, or "".

    A promote does not hand its git commands to the Bash tool -- it runs
    them itself, as its own subprocesses. So the surface cannot always be
    read off the command line: the command may be a bare `git push` whose
    ORIGIN is promote_chathealthy.py several processes up. Asking who our
    ancestors are is the only way to tell a change the operator issued from
    one a gated surface issued on their behalf.

    Bounded by ANCESTRY_DEPTH_LIMIT and by a visited set, because a process
    table read under a race can present a cycle, and a gate that hangs is a
    gate that stops work.
    """
    try:
        import psutil                                       # noqa: PLC0415
    except ImportError:
        return ""
    try:
        current = psutil.Process()
    except Exception:                                        # noqa: BLE001
        return ""

    seen: set[int] = set()
    for _ in range(ANCESTRY_DEPTH_LIMIT):
        try:
            parent = current.parent()
        except Exception:                                    # noqa: BLE001
            return ""
        if parent is None or parent.pid in seen:
            return ""
        seen.add(parent.pid)
        try:
            line = " ".join(parent.cmdline())
        except Exception:                                    # noqa: BLE001
            line = ""
        lowered = line.replace("\\", "/").lower()
        for surface in ALREADY_GATED_SURFACES:
            if surface.lower() in lowered:
                return surface
        current = parent
    return ""


def _already_gated(segment: str, subcommand: str) -> str:
    if subcommand in ALREADY_GATED_SUBCOMMANDS:
        return "the commit-msg gate authorises this commit"
    program = _invoked_program(segment)
    for surface in ALREADY_GATED_SURFACES:
        if program == surface.lower():
            return f"{surface} carries its own authorization gate"
    ancestor = _ancestor_chain_surface()
    if ancestor:
        return f"issued by {ancestor}, which carries its own gate"
    return ""


def _findings(command: str) -> list[dict]:
    """Every segment of the command that changes git state and is not
    already gated elsewhere."""
    found = []
    for segment in _segments(command):
        subcommand = _git_subcommand(segment)
        if not subcommand or subcommand not in MUTATING_SUBCOMMANDS:
            continue
        if _is_read_only_form(segment, subcommand):
            continue
        gated_by = _already_gated(segment, subcommand)
        if gated_by:
            log.info("git state change passed through: %s -- %s",
                     subcommand, gated_by)
            continue
        found.append({
            "segment": segment,
            "subcommand": subcommand,
            "irreversible": subcommand in IRREVERSIBLE,
        })
    return found


def _audit(document: dict) -> None:
    """One record, on the decision.

    There is deliberately no second record on completion. A tool response
    saying a command succeeded is not evidence that it did -- this session
    produced several -- so a completion record would assert something the
    gate cannot know. What it can know is that it asked, whether a human
    answered, and what they said.
    """
    document["requirement"] = REQUIREMENT
    document["component"] = COMPONENT
    try:
        authorization_record.append(document, tolerate_failure=True)
    except Exception as exc:                                  # noqa: BLE001
        log.error("audit record not written: %s: %s",
                  type(exc).__name__, exc,
                  exc=ChatHealthyException(
                      mode="audit_unrecorded",
                      component=COMPONENT,
                      message=f"authorization audit not written: {exc}",
                      exception=exc),
                  if_not_debug_log=True)


def _escape(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;").replace('"', "&quot;"))


def _ask_the_operator(command: str, findings: list[dict]) -> tuple[str, dict]:
    """Serve one local page and block on a human decision.

    Returns (verdict, evidence). Verdict is approve, reject or timeout.
    """
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]

    token = secrets.token_urlsafe(8)
    state: dict = {"verdict": None, "evidence": {}}

    kinds = ", ".join(sorted({f["subcommand"] for f in findings}))
    destructive = any(f["irreversible"] for f in findings)
    banner = ("THIS CANNOT BE UNDONE FROM THIS WORKSTATION"
              if destructive else "This changes git repository state")

    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>ChatHealthy git authorization</title><style>"
        "body{font-family:system-ui,sans-serif;padding:36px;background:#0b7a75;"
        "color:#fff}h1{font-size:24px;margin:0 0 6px}"
        ".warn{background:#7f1d1d;padding:10px 14px;border-radius:6px;"
        "font-weight:700;display:inline-block;margin-bottom:16px}"
        "pre{background:#053e3b;padding:14px;border-radius:6px;text-align:left;"
        "white-space:pre-wrap;word-break:break-all;font-size:13px}"
        "button{font-size:17px;padding:13px 30px;margin:10px 8px 0 0;border:none;"
        "border-radius:6px;font-weight:700}"
        ".approve{background:#0b9a94;color:#fff}.reject{background:#dc2626;color:#fff}"
        "button:disabled{opacity:.35}"
        "#need{font-size:14px;opacity:.9}</style></head><body>"
        f"<div class=warn>{_escape(banner)}</div>"
        f"<h1>Authorize this git command?</h1>"
        f"<p>changes: <b>{_escape(kinds)}</b></p>"
        f"<pre>{_escape(command)}</pre>"
        "<p id=need>Move the mouse to enable the buttons — proof a person is here.</p>"
        "<button class=approve id=ok type=button disabled>APPROVE</button>"
        "<button class=reject id=no type=button disabled>REJECT</button>"
        "<script>"
        f"var T='{token}';var moves=0;var clicked=false;"
        "document.addEventListener('mousemove',function(){moves++;"
        "if(moves>=8){document.getElementById('ok').disabled=false;"
        "document.getElementById('no').disabled=false;"
        "document.getElementById('need').textContent='Movement recorded ('+moves+' events).';}});"
        "document.addEventListener('mousedown',function(){clicked=true;});"
        "function send(v){"
        "fetch('/decide',{method:'POST',body:new URLSearchParams("
        "{token:T,verdict:v,moves:String(moves),clicked:clicked?'true':'false'})})"
        ".then(function(){document.body.innerHTML='<h1>Recorded.</h1>';"
        "try{window.close();}catch(e){}});}"
        "document.getElementById('ok').addEventListener('click',function(){send('approve');});"
        "document.getElementById('no').addEventListener('click',function(){send('reject');});"
        "</script></body></html>"
    )

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def _reply(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):                                     # noqa: N802
            if urlparse(self.path).path in ("/", "/prompt"):
                self._reply(200, page)
            else:
                self._reply(404, "<h1>404</h1>")

        def do_POST(self):                                    # noqa: N802
            if urlparse(self.path).path != "/decide":
                self._reply(404, "<h1>404</h1>")
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
            fields = parse_qs(raw)
            if fields.get("token", [""])[0] != token:
                self._reply(400, "<h1>Bad token</h1>")
                return
            moves = int(fields.get("moves", ["0"])[0] or "0")
            clicked = fields.get("clicked", [""])[0] == "true"
            if moves < 8 or not clicked:
                self._reply(400, "<h1>Refused: no human evidence.</h1>")
                return
            verdict = fields.get("verdict", [""])[0]
            if verdict not in ("approve", "reject"):
                self._reply(400, "<h1>Bad verdict</h1>")
                return
            state["verdict"] = verdict
            state["evidence"] = {"mouse_move_events": moves, "mouse_down": clicked}
            self._reply(200, "<!doctype html><h1>Recorded.</h1>")

    server = socketserver.TCPServer(("127.0.0.1", port), _Handler,
                                    bind_and_activate=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    url = f"http://127.0.0.1:{port}/prompt"
    opened = False
    try:
        opened = webbrowser.open_new(url)
    except Exception:                                          # noqa: BLE001
        opened = False
    if not opened and sys.platform == "win32":
        os.startfile(url)                                      # noqa: S606

    log.info("AWAITING AUTHORIZATION: git %s -- decide at %s", kinds, url)
    sys.stderr.write(f"AWAITING AUTHORIZATION: git {kinds}\n  decide at {url}\n")
    sys.stderr.flush()

    deadline = time.time() + APPROVAL_TIMEOUT_SECONDS
    while time.time() < deadline and state["verdict"] is None:
        time.sleep(0.2)
    server.shutdown()
    server.server_close()

    return (state["verdict"] or "timeout"), state["evidence"]


def main() -> int:
    payload = _hook_payload()
    tool = payload.get("tool_name") or ""
    command = (payload.get("tool_input") or {}).get("command") or ""

    if tool not in ("Bash", "PowerShell") or not command.strip():
        return EXIT_ALLOW

    findings = _findings(command)
    if not findings:
        return EXIT_ALLOW

    verdict, evidence = _ask_the_operator(command, findings)

    _audit({
        "kind": "git_state_change_authorization",
        "asked": True,
        "answered": verdict in ("approve", "reject"),
        "verdict": verdict,
        "tool": tool,
        "command": command,
        "subcommands": sorted({f["subcommand"] for f in findings}),
        "irreversible": any(f["irreversible"] for f in findings),
        "human_evidence": evidence,
        "session_id": payload.get("session_id") or "",
    })

    if verdict == "approve":
        log.info("AUTHORIZATION approve by the operator: git %s (%s mouse events)",
                 ",".join(sorted({f["subcommand"] for f in findings})),
                 evidence.get("mouse_move_events", 0))
        return EXIT_ALLOW

    reason = ("the operator rejected it" if verdict == "reject"
              else f"no decision within {APPROVAL_TIMEOUT_SECONDS}s")

    # This object IS the refusal. Written once, by the worker; the manager
    # carries it through to its own stdout, which is what the harness reads.
    sys.stdout.write(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{REQUIREMENT}: this command changes git repository state "
                f"and {reason}."
            ),
        }
    }) + "\n")
    sys.stdout.flush()
    log.error("AUTHORIZATION %s: git command not run -- %s", verdict, reason,
              exc=ChatHealthyException(
                  mode="security_violation",
                  component=COMPONENT,
                  message=f"git state change denied: {reason}"),
              if_not_debug_log=True)
    sys.stderr.write(
        f"DENIED by {REQUIREMENT}: this command changes git repository state "
        f"and {reason}.\n")
    return EXIT_AFTER_DENY


if __name__ == "__main__":
    sys.exit(main())
