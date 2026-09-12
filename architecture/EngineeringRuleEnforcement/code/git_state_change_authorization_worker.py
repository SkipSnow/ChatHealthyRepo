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
  any other program, able to write, aimed at a tracked path, at .git, or
    at a host git talks to

WHAT DOES NOT.
  PERMITTED_PROGRAMS        the chain surfaces, each carrying its own gate
  PASSED_THROUGH            push and fetch, excluded by the requirement;
                            add and commit, held by the commit-msg gate
  READ_ONLY_SUBCOMMANDS     git subcommands that only read
  READ_ONLY_PROGRAMS        programs that read and report

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
import json
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

    APPROVAL_TIMEOUT_SECONDS = 600
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

    # The permitted programs: each carries its own authorization gate, so
    # the git commands it issues are already answered for.
    PERMITTED_PROGRAMS = (
        "promote_chathealthy.py",
        "build_chathealthy.py",
        "deploy_chathealthy.py",
    )

    # Programs that read and report. The requirement governs CHANGES, so a
    # read is out of scope however often it names a tracked file.
    READ_ONLY_PROGRAMS = frozenset({
        "ls", "dir", "cat", "head", "tail", "less", "more", "nl", "od",
        "grep", "egrep", "fgrep", "rg", "find", "wc", "sort", "uniq", "cut",
        "tr", "awk", "jq", "diff", "cmp", "comm", "stat", "file", "du",
        "basename", "dirname", "realpath", "readlink", "which", "where",
        "echo", "printf", "pwd", "test", "true", "false",
        "get-content", "get-childitem", "select-string", "test-path",
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
    ANCESTRY_DEPTH_LIMIT = 24

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.tool = payload.get("tool_name") or ""
        self.command = (payload.get("tool_input") or {}).get("command") or ""
        self.project_root = next(
            (p for p in _HERE.parents if (p / ".git").exists()),
            _HERE.parents[-1])

    # ── the one entry point ──────────────────────────────────────────────
    def authorize(self) -> int:
        """Decide, record, and return the exit code. Recording is not optional."""
        if self.tool not in ("Bash", "PowerShell") or not self.command.strip():
            return self.EXIT_ALLOW

        findings = self.findings()
        if not findings:
            return self.EXIT_ALLOW

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
        """Every part of the command no allowance covers."""
        found: list[dict] = []

        # A script fed to an interpreter on stdin lives in the command and in
        # no file, so the file walk has nothing to open. It is read here.
        for body in self.heredoc_bodies(self.command):
            why = self.why_source_can_run_git(body, "the inline script")
            if why:
                found.append({"segment": "inline script", "subcommand": why})

        for segment in self.segments(self.command):
            covered = self.permitted_program_for(segment)
            if covered:
                log.info("passed through: %s", covered)
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

            script = self.script_argument(segment)
            if script is not None:
                why = self.why_script_can_run_git(script)
                if why:
                    found.append({"segment": segment,
                                  "subcommand": f"{program}: {why}"})
                continue

            reaches = self.touches_the_repository(segment)
            if reaches:
                found.append({"segment": segment,
                              "subcommand": f"{program}: {reaches}"})
        return found

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
        """The command split at shell separators, so nothing hides behind &&."""
        parts = [self.without_heredoc_bodies(command)]
        for separator in self.SEPARATORS:
            nxt: list[str] = []
            for part in parts:
                nxt.extend(part.split(separator))
            parts = nxt
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

    # ── who issued it ────────────────────────────────────────────────────
    def permitted_program_for(self, segment: str) -> str:
        """The permitted program covering this segment, or ""."""
        program = self.invoked_program(segment)
        for permitted in self.PERMITTED_PROGRAMS:
            if program == permitted.lower():
                return f"{permitted} carries its own authorization gate"
        ancestor = self.ancestor_permitted_program()
        if ancestor:
            return f"issued by {ancestor}, which carries its own gate"
        return ""

    def ancestor_permitted_program(self) -> str:
        """The permitted program this process descends from, or "".

        A promote runs its git commands as its own subprocesses, so the
        surface cannot always be read off the command line.
        """
        try:
            import psutil                                    # noqa: PLC0415
        except ImportError:
            return ""
        try:
            current = psutil.Process()
        except Exception:                                    # noqa: BLE001
            return ""
        seen: set[int] = set()
        for _ in range(self.ANCESTRY_DEPTH_LIMIT):
            try:
                parent = current.parent()
            except Exception:                                # noqa: BLE001
                return ""
            if parent is None or parent.pid in seen:
                return ""
            seen.add(parent.pid)
            try:
                line = " ".join(parent.cmdline())
            except Exception:                                # noqa: BLE001
                line = ""
            # What the ancestor RUNS, not what its command line mentions: a
            # substring test turned the gate off for any process whose line
            # merely contained a permitted name.
            program = self.invoked_program(line)
            for permitted in self.PERMITTED_PROGRAMS:
                if program == permitted.lower():
                    return permitted
            current = parent
        return ""

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

    def tracked_paths(self, candidates: list[str]) -> list[str]:
        """Which of these paths git tracks, asked of git."""
        if not candidates:
            return []
        try:
            result = subprocess.run(
                ["git", "ls-files", "--"] + candidates,
                cwd=str(self.project_root),
                capture_output=True, text=True, timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return candidates
        return [line for line in result.stdout.splitlines() if line.strip()]

    def touches_the_repository(self, segment: str) -> str:
        """Why this segment changes repository state, or "".

        Two things must hold: the program can write, and the target is
        something git owns.
        """
        words = [w.strip("'\"()") for w in segment.split()
                 if not w.startswith("-")]
        lowered = segment.replace("\\", "/").lower()

        for host in self.remote_hosts():
            if host and host in lowered:
                return f"reaches {host}"

        if self.invoked_program(segment) in self.READ_ONLY_PROGRAMS:
            return ""

        if ".git/" in lowered or any(w in (".git", "./.git") for w in words):
            return "reaches .git"

        paths = [w for w in words[1:] if "/" in w or "." in w]
        tracked = self.tracked_paths(paths)
        if tracked:
            return f"writes a tracked file: {tracked[0]}"
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

    def local_imports(self, tree: ast.AST, source: Path) -> list[Path]:
        """Files in this repository that this module imports."""
        modules: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
        found: list[Path] = []
        for dotted in modules:
            tail = dotted.split(".")[-1]
            beside = source.parent / f"{tail}.py"
            if beside.is_file():
                found.append(beside)
                continue
            for candidate in self.project_root.rglob(f"{tail}.py"):
                if (".venv" in candidate.parts
                        or "site-packages" in candidate.parts):
                    continue
                found.append(candidate)
                break
        return found

    def why_source_can_run_git(self, source: str, label: str) -> str:
        """Why this python source can run git, or "" when it cannot."""
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"{label} could not be parsed: {exc}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = self.called_name(node)
            if name in self.DYNAMIC_CALLS:
                return f"{label} executes text at runtime via {name}"
            if name in self.PROCESS_CALLS:
                if self.names_git(node):
                    return f"{label} runs git"
                return f"{label} starts a process via {name}"
        return ""

    def why_script_can_run_git(self, script: Path) -> str:
        """Why this script, or anything local it imports, can run git.

        Python is parsed. Any other language is unprovable here and is
        reported as able to run git.
        """
        if not script.is_file():
            return f"{script} is not a readable file"
        if script.suffix.lower() != ".py":
            return f"{script.suffix or 'this'} is not a language this parses"

        queue: list[tuple[Path, int]] = [(script, 0)]
        seen: set[Path] = set()
        nodes = 0
        while queue:
            path, depth = queue.pop(0)
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if depth > self.WALK_DEPTH_LIMIT:
                return f"{path.name} is deeper than the walk follows"
            try:
                source = path.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (OSError, SyntaxError) as exc:
                return f"{path.name} could not be read: {exc}"

            for node in ast.walk(tree):
                nodes += 1
                if nodes > self.WALK_NODE_LIMIT:
                    return "the dependency tree is larger than the walk follows"
                if not isinstance(node, ast.Call):
                    continue
                name = self.called_name(node)
                if name in self.DYNAMIC_CALLS:
                    return f"{path.name} executes text at runtime via {name}"
                if name in self.PROCESS_CALLS:
                    if self.names_git(node):
                        return f"{path.name} runs git"
                    return f"{path.name} starts a process via {name}"

            for dependency in self.local_imports(tree, path):
                queue.append((dependency, depth + 1))
        return ""

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
