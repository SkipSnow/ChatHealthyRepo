# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""EPIC-008-F-002-S-013-REQ-B-001 — a human approves every Claude-issued
command that changes the state of a ChatHealthy git repository.

SCOPE. Changes to repository state, and nothing else. The local filesystem
is out of scope until git tracks a file. A read is out of scope whatever it
names.

WHAT REACHES THE OPERATOR.
  a git subcommand that writes
  a gh command
  a script that can invoke git, itself or through what it imports
  an HTTP client aimed at a host git talks to, which is the Git API

WHAT DOES NOT. Anything that is not one of those four. A command is judged
on the program it runs, never on the files it names: git is the only thing
that changes repository state, so a program that is not git and cannot
reach git is out of scope no matter which path appears on its line.

  PASSED_THROUGH            push and fetch, excluded by the requirement;
                            add and commit, held by the commit-msg gate
  READ_ONLY_SUBCOMMANDS     git subcommands that only read

NO DOUBLE GOVERNANCE IS SATISFIED BY THE EXEMPTIONS, NOT BY THIS CODE. The
requirement allows a promote local->dev two gates and every other change
exactly one, so the programs that carry their own gate must never reach
here. They are declared in the scopes array on Rule-006-ENF-001 in
engineering_rules.json. This class does not name them and cannot tell that
a command came from one: that half of the requirement is met by the
exemption being honoured before this worker is dispatched.

THE DECISION AND ITS RECORD ARE ONE ACT. `authorize` writes the audit and
returns the outcome in the same step: there is no path through this class
that yields a verdict nothing recorded. An unrecorded authorization did not
happen, so it denies.

EVIDENCE OF A HUMAN. The approval page records mousemove as well as the
click on the button. A click can be synthesised by script in the page; a
trail of movement arriving before it is what distinguishes a person at the
machine from anything that merely posts the form.
"""
from __future__ import annotations

import ast
import importlib.util
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


class GitStateChangeAuthorization:
    """Asks a human before a command changes this repository, and records it."""

    COMPONENT = "git_state_change_authorization_worker"
    REQUIREMENT = "EPIC-008-F-002-S-013-REQ-B-001"

    # A refusal travels as JSON on stdout and the process exits 0. Exit 2 is
    # the other documented way to block and cannot be used: the manager
    # defines EXIT_WORKER_ERROR = 2, reads a 2 as a crashed worker, promotes
    # it to 5, and 5 does not block.
    EXIT_ALLOW = 0
    EXIT_AFTER_DENY = 0

    # The budget the manager handed down. One value, declared once, in
    # the manager; this class holds no number of its own. An expiry is a
    # rejection: the command does not run and the refusal is recorded.
    #
    # Read from the environment rather than inherited because this class
    # has no base class, by instruction.
    APPROVAL_TIMEOUT_SECONDS = int(
        os.environ.get("CHATHEALTHY_ENFORCEMENT_TIMEOUT_SECONDS") or 0)
    AUDIT_UNRECORDED = "CH-GATE-001 audit record could not be written to MongoDB"

    # Git subcommands that only read.
    READ_ONLY_SUBCOMMANDS = frozenset({
        "status", "log", "diff", "show", "rev-parse", "rev-list", "ls-files",
        "ls-tree", "ls-remote", "cat-file", "describe", "blame", "shortlog",
        "grep", "check-ignore", "check-attr", "count-objects", "for-each-ref",
        "name-rev", "merge-base", "verify-commit", "verify-tag", "whatchanged",
        "version", "help", "var", "difftool",
    })

    # Git subcommands the operator has already answered for. push and fetch
    # are excluded by the requirement in terms; add and commit are held by
    # the commit-msg gate, and gating them here would be two prompts for one
    # act.
    PASSED_THROUGH = frozenset({"push", "fetch", "add", "commit"})

    # Programs that speak HTTP. One of these aimed at a host git talks to is
    # the Git API, which the requirement puts in scope alongside the command.
    NETWORK_CLIENTS = frozenset({
        "curl", "wget", "http", "httpie",
        "invoke-webrequest", "invoke-restmethod", "iwr", "irm",
    })

    # Programs that run a script file handed to them. A script named
    # anywhere else on the line is an argument, not something executed:
    # `grep pattern thing.py` reads thing.py and runs nothing.
    INTERPRETERS = frozenset({
        "python", "python.exe", "python3", "py", "node", "node.exe",
        "bash", "sh", "zsh", "pwsh", "powershell", "powershell.exe",
        "perl", "ruby",
    })
    SCRIPT_SUFFIXES = (".py", ".sh", ".ps1", ".js", ".rb", ".pl")

    # Calls that hand a command line to the operating system.
    PROCESS_CALLS = frozenset({
        "run", "call", "check_call", "check_output", "Popen",
        "system", "popen", "spawn", "spawnl", "spawnv", "spawnve",
        "execv", "execve", "execvp", "execl", "execlp", "startfile",
    })

    # Calls that execute text, so what they do cannot be read off the source.
    DYNAMIC_CALLS = frozenset({
        "exec", "eval", "compile", "import_module", "run_module",
        "run_path", "__import__", "load_source",
    })

    # Bounds so a cycle or a vast tree cannot hang the gate. High enough that
    # a real script finishes: hitting a bound reports the script unprovable,
    # so a low bound turns ordinary work into questions.
    WALK_NODE_LIMIT = 50_000
    WALK_DEPTH_LIMIT = 16

    SEPARATORS = ("&&", "||", ";", "|", "\n")

    ENFORCEMENT_ID = "Rule-006-ENF-001"

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.tool = payload.get("tool_name") or ""
        self.command = (payload.get("tool_input") or {}).get("command") or ""
        self.project_root = next(
            (p for p in _HERE.parents if (p / ".git").exists()),
            _HERE.parents[-1])
        self.exempt = self._exempt_programs()

    def _exempt_programs(self) -> frozenset[str]:
        """The programs this enforcement is told never to examine.

        Read from the scopes rows on this enforcement in
        engineering_rules.json. The rule holds the list; this class holds
        none, so exempting a program is an edit to the rule and never to
        the code.
        """
        # Read from HEAD, never the working tree. An exemption in the tree
        # is a sentence the constrained actor wrote a moment ago; one at
        # HEAD has passed the human-gated commit. Slipping an exception
        # into the file therefore buys nothing until a human approves the
        # commit that carries it. Unreadable means no exemptions.
        try:
            shown = subprocess.run(
                ["git", "show",
                 "HEAD:brain/machine_artifacts/content/engineering_rules.json"],
                cwd=str(self.project_root), capture_output=True, text=True,
                timeout=10)
            content = json.loads(shown.stdout)
        except (OSError, ValueError, subprocess.SubprocessError):
            return frozenset()
        names: set[str] = set()
        for rule in content.get("rules", {}).get("rule", []):
            enforcements = (rule.get("enforcements") or {}).get("enforcement", [])
            for enforcement in enforcements:
                if enforcement.get("enforcement_id") != self.ENFORCEMENT_ID:
                    continue
                for row in enforcement.get("scopes") or []:
                    if len(row) == 3 and row[1] == "excluded_exact":
                        names.update(str(term).lower() for term in row[2])
        return frozenset(names)

    # ── the one entry point ──────────────────────────────────────────────
    def authorize(self) -> int:
        """Decide, record, and return the exit code. Recording is not optional.

        Every command is judged, whatever tool carried it. Naming the tools
        this governs was the defect: the name list here and the matcher in
        the hook wiring were two statements of one scope, and the wiring
        silently won -- so this claimed PowerShell and never saw it.

        There is one evaluation. findings() judges every tool call --
        command or file write -- and this method carries any finding
        through the one ask/record/deny sequence. A second decision branch
        here is how the file-tool bypass survived the first rewrite.
        """
        findings = self.findings()
        if not findings:
            return self.EXIT_ALLOW

        # Untestable software is not Claude's to run and not a human's to
        # bless from a popup: a yes cannot make the unprovable proven. It
        # denies outright, recorded, and the one way back is the exception
        # list on Rule-006-ENF-001 -- an edit that itself takes a
        # human-approved commit before this gate honours it.
        untestable = [f for f in findings if f.get("untestable")]
        if untestable:
            if not self.record("untestable", {}, findings):
                return self.deny(self.AUDIT_UNRECORDED, "unrecorded")
            what = "; ".join(sorted({f["subcommand"] for f in untestable}))
            return self.deny(
                f"this executes software the walk cannot fully test: {what}. "
                "Untestable software may not run; the remedy is the "
                "exception list on Rule-006-ENF-001, via an approved commit.",
                "untestable")

        verdict, evidence = self.ask_the_operator(findings)

        if not self.record(verdict, evidence, findings):
            return self.deny(self.AUDIT_UNRECORDED, "unrecorded")

        if verdict == "approve":
            log.info("AUTHORIZATION approve: %s (%s mouse events)",
                     ",".join(sorted({f["subcommand"] for f in findings})),
                     evidence.get("mouse_move_events", 0))
            return self.EXIT_ALLOW

        reason = ("the operator rejected it" if verdict == "reject"
                  else f"no decision within {self.APPROVAL_TIMEOUT_SECONDS}s")
        return self.deny(reason, verdict)

    def main(self) -> int:
        """What the enforcement manager invokes."""
        return self.authorize()

    # ── the record ───────────────────────────────────────────────────────
    def record(self, verdict: str, evidence: dict, findings: list[dict]) -> bool:
        """Write the audit. False when it could not be written.

        The requirement says a record must be made on approval, timeout and
        disapproval. A record that could not be written is not a record.
        """
        document = {
            "kind": "git_state_change_authorization",
            "asked": True,
            "answered": verdict in ("approve", "reject"),
            "verdict": verdict,
            "tool": self.tool,
            "command": self.command,
            "subcommands": sorted({f["subcommand"] for f in findings}),
            "human_evidence": evidence,
            "session_id": self.payload.get("session_id") or "",
            "requirement": self.REQUIREMENT,
            "component": self.COMPONENT,
        }
        try:
            authorization_record.append(document, tolerate_failure=False)
            return True
        except Exception as exc:                              # noqa: BLE001
            log.error("audit record not written: %s: %s",
                      type(exc).__name__, exc,
                      exc=ChatHealthyException(
                          mode="audit_unrecorded",
                          component=self.COMPONENT,
                          message=f"authorization audit not written: {exc}",
                          exception=exc),
                      if_not_debug_log=True)
            return False

    # ── the refusal ──────────────────────────────────────────────────────
    def deny(self, reason: str, verdict: str = "fatal") -> int:
        """Refuse the command. The only thing that stops anything."""
        sys.stdout.write(json.dumps({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    f"{self.REQUIREMENT}: this command changes git repository "
                    f"state and {reason}."
                ),
            }
        }) + "\n")
        sys.stdout.flush()
        log.error("AUTHORIZATION %s: command not run -- %s", verdict, reason,
                  exc=ChatHealthyException(
                      mode="security_violation",
                      component=self.COMPONENT,
                      message=f"git state change denied: {reason}"),
                  if_not_debug_log=True)
        sys.stderr.write(
            f"DENIED by {self.REQUIREMENT}: this command changes git "
            f"repository state and {reason}.\n")
        sys.stderr.flush()
        return self.EXIT_AFTER_DENY

    # ── what the command is ──────────────────────────────────────────────
    def findings(self) -> list[dict]:
        """Every part of the tool call no allowance covers.

        The one evaluation. A call with no command is judged on its path;
        a call with a command is judged segment by segment, and every
        script it executes is walked to its last leaf with every reason
        collected -- stopping at the first would leave the rest of the
        tree untested and the operator deciding on a partial account.
        """
        found: list[dict] = []

        if not self.command.strip():
            reach = self.file_write_into_git()
            if reach:
                found.append({"segment": reach,
                              "subcommand": "writes inside .git"})
            return found

        # A script fed to an interpreter on stdin lives in the command and in
        # no file, so the file walk has nothing to open. It is read here.
        for body in self.heredoc_bodies(self.command):
            for why, untestable in self.why_content_can_run_git(
                    body, "the inline script"):
                found.append({"segment": "inline script", "subcommand": why,
                              "untestable": untestable})

        for segment in self.segments(self.command):
            # An exempt program is not examined at all. The rule says which,
            # and a program it names never reaches the checks below.
            if self.invoked_program(segment) in self.exempt:
                continue

            subcommand = self.git_subcommand(segment)
            if subcommand:
                if subcommand in self.READ_ONLY_SUBCOMMANDS:
                    continue
                if subcommand in self.PASSED_THROUGH:
                    continue
                if self.is_read_only_form(segment, subcommand):
                    continue
                found.append({"segment": segment, "subcommand": subcommand})
                continue

            gh_command = self.github_cli_invocation(segment)
            if gh_command:
                found.append({"segment": segment,
                              "subcommand": f"gh {gh_command}"})
                continue

            program = self.invoked_program(segment)
            if not program:
                continue

            interpreter = self.interpreter_head(segment)
            if interpreter:
                for why, untestable in self.inline_execution(segment):
                    found.append({"segment": segment,
                                  "subcommand": f"{interpreter}: {why}",
                                  "untestable": untestable})

            script = self.script_argument(segment)
            if script is not None:
                for why, untestable in self.why_script_can_run_git(script):
                    found.append({"segment": segment,
                                  "subcommand": f"{program}: {why}",
                                  "untestable": untestable})
                continue

            reaches = self.reaches_the_git_api(segment)
            if reaches:
                found.append({"segment": segment,
                              "subcommand": f"{program}: {reaches}"})
        return found

    def inline_execution(self, segment: str) -> list[tuple[str, bool]]:
        """Code an interpreter runs that lives in no script file.

        -c hands the interpreter source on the command line and -m hands
        it a module name. Both execute software, so both are read: python
        -c source is parsed like any other content, a -m module is
        resolved to its file and walked, and what cannot be read or
        resolved is untestable. Another interpreter's -c/-e is a language
        this cannot parse, so it is untestable too.
        """
        words = segment.split()
        program = self.interpreter_head(segment)
        is_python = program.startswith("py")
        reasons: list[tuple[str, bool]] = []

        for flag in ("-c", "-e"):
            if flag not in words:
                continue
            if not is_python:
                reasons.append(
                    (f"{flag} hands {program} code this walk cannot parse",
                     True))
                continue
            marker = f" {flag} "
            code = segment.split(marker, 1)[1].strip() if marker in segment \
                else ""
            code = code.strip("'\"")
            if not code:
                reasons.append((f"{flag} with nothing after it", True))
            else:
                reasons.extend(
                    self.why_content_can_run_git(code, f"the {flag} code"))

        if "-m" in words:
            index = words.index("-m")
            module = words[index + 1] if index + 1 < len(words) else ""
            if not is_python or not module:
                reasons.append(("-m names software this walk cannot resolve",
                                True))
            elif module.split(".")[0] in sys.stdlib_module_names:
                pass
            else:
                tail = module.split(".")[-1]
                matches = [p for p in self.project_root.rglob(f"{tail}.py")
                           if ".venv" not in p.parts
                           and "site-packages" not in p.parts
                           and "node_modules" not in p.parts]
                if not matches:
                    installed = self.installed_module_file(module)
                    if installed is not None:
                        matches.append(installed)
                if not matches:
                    reasons.append(
                        (f"-m {module} resolves to no file this walk can "
                         "read", True))
                for match in matches:
                    reasons.extend(self.why_script_can_run_git(match))
        return reasons

    def file_write_into_git(self) -> str:
        """The .git path this tool call writes, or "" when it writes none.

        Judged the way a command is: on the program and the target. The
        allowance names the tools that only read -- everything else that
        aims a path under .git gates, so a writing tool this list has
        never heard of fails closed instead of passing unnamed. The path
        is judged resolved, so a relative spelling or a traversal cannot
        hide the destination.
        """
        if self.tool in ("Read", "Grep", "Glob"):
            return ""
        tool_input = self.payload.get("tool_input") or {}
        for field in ("file_path", "notebook_path", "path"):
            raw = str(tool_input.get(field) or "").strip()
            if not raw:
                continue
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = self.project_root / raw
            try:
                resolved = candidate.resolve()
            except OSError:
                return raw
            if ".git" in resolved.parts:
                return str(resolved)
        return ""

    # ── reading the command ──────────────────────────────────────────────
    def without_heredoc_bodies(self, command: str) -> str:
        """The command with heredoc bodies removed.

        A heredoc body is data handed to a program's stdin, not a line this
        shell runs. The `<<` line itself is kept, because that IS a command.
        """
        kept: list[str] = []
        delimiter = ""
        for line in command.split("\n"):
            if delimiter:
                if line.strip() == delimiter:
                    delimiter = ""
                continue
            kept.append(line)
            if "<<" not in line:
                continue
            tail = line.split("<<", 1)[1].lstrip()
            if tail.startswith("<"):
                continue
            if tail.startswith("-"):
                tail = tail[1:]
            word = tail.split()[0] if tail.split() else ""
            delimiter = word.strip("'\"")
        return "\n".join(kept)

    def heredoc_bodies(self, command: str) -> list[str]:
        """The script bodies fed to an INTERPRETER on stdin.

        A heredoc body is only a script when the program reading it runs
        code. `git commit -F -` and `gh api --input -` are handed data, and
        a commit message read as python parses as nothing.
        """
        bodies: list[str] = []
        collecting: list[str] = []
        delimiter = ""
        for line in command.split("\n"):
            if delimiter:
                if line.strip() == delimiter:
                    bodies.append("\n".join(collecting))
                    collecting = []
                    delimiter = ""
                else:
                    collecting.append(line)
                continue
            if "<<" not in line:
                continue
            before = line.split("<<", 1)[0]
            if self.invoked_program(before) not in self.INTERPRETERS:
                continue
            tail = line.split("<<", 1)[1].lstrip()
            if tail.startswith("<"):
                continue
            if tail.startswith("-"):
                tail = tail[1:]
            word = tail.split()[0] if tail.split() else ""
            if word:
                delimiter = word.strip("'\"")
        if collecting:
            bodies.append("\n".join(collecting))
        return bodies

    def segments(self, command: str) -> list[str]:
        """The command split at shell separators, so nothing hides behind &&.

        Separators inside quotes are text, not separators. Splitting through
        them tore `grep "a\\|b"` into a fragment beginning `b"`, which is no
        program anyone allowed, so a search became a question.

        A backslash at end of line continues the command, so the newline
        after it is not a separator. Treating it as one turned every
        continued argument into an apparent command: a `git log` whose paths
        were listed one per line became several segments, and a path ending
        .py read as a script being executed, so a read was walked and gated.
        """
        text = self.without_heredoc_bodies(command)
        text = text.replace("\\\r\n", " ").replace("\\\n", " ")
        parts: list[str] = []
        current: list[str] = []
        quote = ""
        index = 0
        while index < len(text):
            char = text[index]
            if quote:
                current.append(char)
                if char == quote:
                    quote = ""
                index += 1
                continue
            if char in ("'", '"'):
                quote = char
                current.append(char)
                index += 1
                continue
            matched = ""
            for separator in self.SEPARATORS:
                if text.startswith(separator, index):
                    matched = separator
                    break
            if matched:
                parts.append("".join(current))
                current = []
                index += len(matched)
                continue
            current.append(char)
            index += 1
        parts.append("".join(current))
        return [p.strip() for p in parts if p.strip()]

    def git_subcommand(self, segment: str) -> str:
        """The subcommand of a git invocation, or "" when it is not one."""
        words = segment.split()
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
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
                continue
            if word in ("-C", "-c"):
                continue
            return word.lower()
        return ""

    def github_cli_invocation(self, segment: str) -> str:
        """The gh command being run, or "" when the segment is not one."""
        words = segment.split()
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        if not words:
            return ""
        head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if head not in ("gh", "gh.exe"):
            return ""
        for word in words[1:]:
            if word.startswith("-"):
                continue
            return word.lower()
        return "gh"

    def interpreter_head(self, segment: str) -> str:
        """The interpreter this segment starts with, or "".

        invoked_program looks through an interpreter to name the script it
        runs, which is right for exemptions and wrong for spotting -c and
        -m: those have no script, so looking through the interpreter
        returns whatever word follows the flag. This reads the literal
        head and nothing else.
        """
        words = segment.split()
        while words and "=" in words[0] and not words[0].startswith("-"):
            words = words[1:]
        if not words:
            return ""
        head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        return head if head in self.INTERPRETERS else ""

    def invoked_program(self, segment: str) -> str:
        """The program this segment runs, bare of its directory."""
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

    READ_ONLY_FLAGS = frozenset({
        "-l", "--list", "-v", "--verbose", "-n", "--dry-run", "--get",
        "--get-all", "--get-regexp", "--show-current", "--points-at",
        "--contains", "--merged", "--no-merged", "--show-toplevel",
    })

    def is_read_only_form(self, segment: str, subcommand: str) -> bool:
        """Some subcommands read or write depending on their flags."""
        if subcommand not in ("tag", "branch", "remote", "notes", "stash",
                              "submodule", "config", "worktree", "reflog"):
            return False
        words = segment.split()
        for word in words:
            if word in self.READ_ONLY_FLAGS:
                return True
        if subcommand == "stash" and "list" in words:
            return True
        if subcommand in ("remote", "worktree", "submodule") and len(words) <= 2:
            return True
        return False


    # ── what it reaches ──────────────────────────────────────────────────
    def remote_hosts(self) -> tuple[str, ...]:
        """The hosts git is configured to talk to, asked of git."""
        try:
            result = subprocess.run(
                ["git", "remote", "-v"], cwd=str(self.project_root),
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return ()
        hosts: set[str] = set()
        for line in result.stdout.splitlines():
            for word in line.split():
                if "://" in word:
                    hosts.add(word.split("://", 1)[1].split("/", 1)[0].lower())
                elif "@" in word and ":" in word:
                    hosts.add(word.split("@", 1)[1].split(":", 1)[0].lower())
        return tuple(sorted(hosts))

    def reaches_the_git_api(self, segment: str) -> str:
        """Why this segment reaches the Git API over the network, or "".

        The requirement puts the Git API in scope beside the command, and an
        HTTP client aimed at a host git talks to is that API. Both halves are
        required: a client that names no such host is doing something else,
        and a host named by anything other than a client is a string, not a
        request.
        """
        if self.invoked_program(segment) not in self.NETWORK_CLIENTS:
            return ""
        lowered = segment.replace("\\", "/").lower()
        for host in self.remote_hosts():
            if host and host in lowered:
                return f"reaches {host}"
        return ""

    # ── what a script would do ───────────────────────────────────────────
    def script_argument(self, segment: str) -> Path | None:
        """The script this segment executes, or None when it executes none."""
        words = [w for w in segment.split()
                 if "=" not in w or w.startswith("-")]
        if not words:
            return None
        head = words[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if head in self.INTERPRETERS:
            candidates = words[1:]
        elif head.endswith(self.SCRIPT_SUFFIXES):
            candidates = words[:1]
        else:
            return None
        for word in candidates:
            if word.startswith("-"):
                continue
            bare = word.strip("'\"")
            if bare.lower().endswith(self.SCRIPT_SUFFIXES):
                return (Path(bare) if Path(bare).is_absolute()
                        else self.project_root / bare)
        return None

    def names_git(self, node: ast.AST) -> bool:
        """True when a literal under this node names the git program."""
        for child in ast.walk(node):
            if isinstance(child, ast.Constant) and isinstance(child.value, str):
                text = child.value.strip().lower()
                if text == "git" or text.startswith("git "):
                    return True
                if text.endswith("/git") or text.endswith("\\git"):
                    return True
                if "git.exe" in text:
                    return True
        return False

    def called_name(self, call: ast.Call) -> str:
        """The bare name of what a Call invokes."""
        target = call.func
        if isinstance(target, ast.Attribute):
            return target.attr
        if isinstance(target, ast.Name):
            return target.id
        return ""

    def local_imports(self, tree: ast.AST,
                      source: Path) -> tuple[list[Path], list[str]]:
        """Every import, accounted: the files to walk, and the names that
        cannot be.

        Each import lands in exactly one of three places. A module the
        project holds is a branch and is walked -- every file matching the
        name, because picking one of several is guessing which branch
        grows here. A module of the interpreter's own standard library is
        the platform, not project code, and is the one boundary the walk
        accepts. Everything else is a limb this walk cannot parse, and an
        unparsed limb is a failure, not a pass.
        """
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        found: list[Path] = []
        unparseable: list[str] = []
        for dotted in modules:
            top = dotted.split(".")[0]
            if top in sys.stdlib_module_names:
                continue
            tail = dotted.split(".")[-1]
            matches: list[Path] = []
            beside = source.parent / f"{tail}.py"
            if beside.is_file():
                matches.append(beside)
            for candidate in self.project_root.rglob(f"{tail}.py"):
                if (".venv" in candidate.parts
                        or "site-packages" in candidate.parts
                        or "node_modules" in candidate.parts):
                    continue
                matches.append(candidate)
            if not matches:
                # Not the project's and not the interpreter's: installed
                # code. Installed Python is still Python and is read like
                # any other branch. Only a module with no source file to
                # read is truly unparseable.
                installed = self.installed_module_file(dotted)
                if installed is not None:
                    matches.append(installed)
            if matches:
                found.extend(matches)
            else:
                unparseable.append(dotted)
        return found, unparseable

    def installed_module_file(self, dotted: str) -> Path | None:
        """The source file of an installed module, or None.

        Located without importing it: importing executes, and the walk
        must never run what it is reading. Compiled extensions and
        namespace packages have no source to read and return None, which
        the caller reports as unparseable.
        """
        try:
            spec = importlib.util.find_spec(dotted.split(".")[0])
        except (ImportError, ValueError, AttributeError):
            return None
        origin = getattr(spec, "origin", None) if spec else None
        if not origin or not str(origin).endswith(".py"):
            return None
        return Path(origin)

    def why_content_can_run_git(self, source: str, label: str,
                                origin: Path | None = None
                                ) -> list[tuple[str, bool]]:
        """Every reason this content can run git; empty when it cannot.

        The one place content is judged. A command's inline script and a
        script file are the same question asked of different bytes, and a
        file's imports are more content, so all three arrive here. Nothing
        else inspects content: a second judge is a second answer waiting to
        disagree with this one.

        The walk parses the tree to its most remote branch and leaf and
        tests every command it holds. It does not stop at the first
        finding: a partial walk is a partial account, and the operator
        decides on the whole of it.

        Each reason carries whether it is untestable. A literal git call is
        provable and a human may approve it. Text executed at runtime, a
        process built at runtime, a limb that cannot be read, resolved or
        parsed -- none of these can be tested, and untestable software may
        not run: those deny without asking, and the remedy is the rule's
        exception list, not a popup.

        `origin` is where the content was read from, and is None for content
        that lives in the command rather than a file. Imports are followed
        only when there is an origin to resolve them against.
        """
        reasons: list[tuple[str, bool]] = []
        queue: list[tuple[str, str, Path | None, int]] = [
            (source, label, origin, 0)]
        seen: set[Path] = set()
        nodes = 0
        while queue:
            text, tag, path, depth = queue.pop(0)
            if path is not None:
                resolved = path.resolve()
                if resolved in seen:
                    continue
                seen.add(resolved)
            if depth > self.WALK_DEPTH_LIMIT:
                reasons.append((f"{tag} is deeper than the walk follows", True))
                continue
            try:
                tree = ast.parse(text)
            except SyntaxError as exc:
                reasons.append((f"{tag} could not be parsed: {exc}", True))
                continue

            for node in ast.walk(tree):
                nodes += 1
                if nodes > self.WALK_NODE_LIMIT:
                    reasons.append(
                        ("the dependency tree is larger than the walk follows",
                         True))
                    return reasons
                if not isinstance(node, ast.Call):
                    continue
                name = self.called_name(node)
                if name in self.DYNAMIC_CALLS:
                    reasons.append(
                        (f"{tag} executes text at runtime via {name}", True))
                elif name in self.PROCESS_CALLS:
                    if self.names_git(node):
                        reasons.append((f"{tag} runs git", False))
                    else:
                        reasons.append(
                            (f"{tag} builds a process at runtime via {name}",
                             True))

            if path is None:
                continue
            walkable, unparseable = self.local_imports(tree, path)
            for dotted in unparseable:
                reasons.append(
                    (f"{tag} imports {dotted}, which this walk cannot parse",
                     True))
            for dependency in walkable:
                try:
                    read = dependency.read_text(encoding="utf-8",
                                                errors="replace")
                except OSError as exc:
                    reasons.append(
                        (f"{dependency.name} could not be read: {exc}", True))
                    continue
                queue.append((read, dependency.name, dependency, depth + 1))
        return reasons

    def why_script_can_run_git(self, script: Path) -> list[tuple[str, bool]]:
        """A script file, read and handed to the one content check.

        Python is parsed. Any other language is untestable here.
        """
        if not script.is_file():
            return [(f"{script} is not a readable file", True)]
        if script.suffix.lower() != ".py":
            return [(f"{script.suffix or 'this'} is not a language this "
                     "parses", True)]
        try:
            source = script.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return [(f"{script.name} could not be read: {exc}", True)]
        return self.why_content_can_run_git(source, script.name, script)

    # ── the human ────────────────────────────────────────────────────────
    def escape(self, text: str) -> str:
        return (text.replace("&", "&amp;").replace("<", "&lt;")
                    .replace(">", "&gt;").replace('"', "&quot;"))

    def ask_the_operator(self, findings: list[dict]) -> tuple[str, dict]:
        """Serve one local page and block on a human decision."""
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

        token = secrets.token_urlsafe(8)
        state: dict = {"verdict": None, "evidence": {}}
        kinds = ", ".join(sorted({f["subcommand"] for f in findings}))
        banner = "THIS CHANGES THE REPOSITORY AND MAY NOT BE UNDOABLE"

        page = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>ChatHealthy git authorization</title><style>"
            "body{font-family:system-ui,sans-serif;padding:36px;"
            "background:#0b7a75;color:#fff}h1{font-size:24px;margin:0 0 6px}"
            ".warn{background:#7f1d1d;padding:10px 14px;border-radius:6px;"
            "font-weight:700;display:inline-block;margin-bottom:16px}"
            "pre{background:#04433f;padding:14px;border-radius:6px;"
            "white-space:pre-wrap;word-break:break-word;font-size:14px}"
            "button{font-size:18px;padding:14px 34px;margin:8px 8px 0 0;"
            "border:none;border-radius:6px;cursor:pointer;font-weight:700}"
            ".approve{background:#0f766e;color:#fff}"
            ".reject{background:#b91c1c;color:#fff}"
            "#why{margin:12px 0;color:#ffd7d5;font-size:15px;min-height:20px}"
            "</style></head><body>"
            f"<div class=warn>{banner}</div>"
            f"<h1>Authorize this command?</h1>"
            f"<p>{self.escape(kinds)}</p>"
            f"<pre>{self.escape(self.command)}</pre>"
            "<button class=approve id=btn_approve type=button>APPROVE</button>"
            "<button class=reject id=btn_reject type=button>REJECT</button>"
            "<div id=why></div>"
            "<input type=hidden id=human_click value=\"false\">"
            "<script>"
            f"var TOKEN='{token}';"
            "var moved=0,lx=null,ly=null,pressed=false,events=0;"
            "document.addEventListener('mousemove',function(e){"
            "if(!e.isTrusted)return;events++;"
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
            "{token:TOKEN,verdict:v,human_click:hc,events:String(events)})})"
            ".then(function(){document.body.innerHTML='<h1>Recorded.</h1>';"
            "try{window.close();}catch(e){}});}"
            "document.getElementById('btn_approve').addEventListener('click',"
            "function(){send('approve');});"
            "document.getElementById('btn_reject').addEventListener('click',"
            "function(){send('reject');});"
            "</script></body></html>"
        )

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                return

            def _send(self, code: int, body: str) -> None:
                raw = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):                                # noqa: N802
                if urlparse(self.path).path == "/prompt":
                    self._send(200, page)
                else:
                    self._send(404, "<h1>404</h1>")

            def do_POST(self):                               # noqa: N802
                if urlparse(self.path).path != "/decide":
                    self._send(404, "<h1>404</h1>")
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = (self.rfile.read(length).decode("utf-8", "replace")
                       if length else "")
                fields = parse_qs(raw)
                if fields.get("token", [""])[0] != token:
                    self._send(400, "<h1>Bad token</h1>")
                    return
                if fields.get("human_click", [""])[0] != "true":
                    self._send(400, "<h1>A real mouse click is required.</h1>")
                    return
                verdict = fields.get("verdict", [""])[0]
                if verdict not in ("approve", "reject"):
                    self._send(400, "<h1>Bad verdict</h1>")
                    return
                state["verdict"] = verdict
                try:
                    events = int(fields.get("events", ["0"])[0] or 0)
                except ValueError:
                    events = 0
                state["evidence"] = {"mouse_move_events": events,
                                     "mouse_down": True}
                self._send(200, "<h1>Recorded.</h1>")

        url = f"http://127.0.0.1:{port}/prompt"
        try:
            server = socketserver.TCPServer(("127.0.0.1", port), Handler,
                                            bind_and_activate=True)
        except OSError:
            return "timeout", {}

        threading.Thread(target=server.serve_forever, daemon=True).start()
        sys.stderr.write(f"\nAuthorization requested at: {url}\n")
        sys.stderr.flush()
        try:
            webbrowser.open(url)
        except Exception:                                    # noqa: BLE001
            pass

        deadline = time.time() + self.APPROVAL_TIMEOUT_SECONDS
        while time.time() < deadline and state["verdict"] is None:
            time.sleep(0.25)
        server.shutdown()
        # An unanswered request is a rejection. Silence is never consent:
        # the command does not run, and the refusal is recorded with the
        # same audit record an explicit reject gets.
        return (state["verdict"] or "timeout"), state["evidence"]


def _payload() -> dict:
    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    if not raw.strip():
        return {}
    try:
        return json.loads(raw)
    except ValueError:
        return {}


def main() -> int:
    return GitStateChangeAuthorization(_payload()).main()


if __name__ == "__main__":
    sys.exit(main())
