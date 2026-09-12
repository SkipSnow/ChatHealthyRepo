# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Whether a script, and everything it reaches, can run a git command.

EPIC-008-F-002-S-013-REQ-B-001. The authorization gate sees the command
Claude hands the Bash tool. It does not see what a script does once it
starts, so approving `python thing.py` approves every git command inside
thing.py without the operator seeing one of them.

This module reads the script before the question is asked. A script is
allowed only when its whole local dependency tree is proven unable to run
a git command. Anything this module cannot prove is treated as able to:
an unreadable file, a language it cannot parse, a computed command, a
call into code that is not in this repository.

Python is parsed with ast. Every other language is unprovable here and is
therefore reported as able to run git.
"""
from __future__ import annotations

import ast
from pathlib import Path

# Calls that hand a command line to the operating system.
_PROCESS_CALLS = frozenset({
    "run", "call", "check_call", "check_output", "Popen",
    "system", "popen", "spawn", "spawnl", "spawnv", "spawnve",
    "execv", "execve", "execvp", "execl", "execlp", "startfile",
})

# Calls that execute text, so what they do cannot be read off the source.
_DYNAMIC_CALLS = frozenset({
    "exec", "eval", "compile", "import_module", "run_module",
    "run_path", "__import__", "load_source",
})

WALK_NODE_LIMIT = 400
WALK_DEPTH_LIMIT = 8


class Verdict:
    """Why a script can run git, or that it cannot."""

    def __init__(self, can_run_git: bool, reason: str = "") -> None:
        self.can_run_git = can_run_git
        self.reason = reason


def _names_git(node: ast.AST) -> bool:
    """True when a literal anywhere under this node names the git program."""
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


def _called_name(call: ast.Call) -> str:
    """The bare name of what a Call invokes."""
    target = call.func
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Name):
        return target.id
    return ""


def _local_imports(tree: ast.AST, source: Path, repo_root: Path) -> list[Path]:
    """Files in this repository that this module imports."""
    found: list[Path] = []
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    for dotted in modules:
        tail = dotted.split(".")[-1]
        beside = source.parent / f"{tail}.py"
        if beside.is_file():
            found.append(beside)
            continue
        for candidate in repo_root.rglob(f"{tail}.py"):
            if ".venv" in candidate.parts or "site-packages" in candidate.parts:
                continue
            found.append(candidate)
            break
    return found


def _inspect_python(source: Path, repo_root: Path) -> Verdict:
    """One python file and everything local it imports."""
    queue: list[tuple[Path, int]] = [(source, 0)]
    seen: set[Path] = set()
    nodes = 0

    while queue:
        path, depth = queue.pop(0)
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if depth > WALK_DEPTH_LIMIT:
            return Verdict(True, f"{path.name} is deeper than the walk follows")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError) as exc:
            return Verdict(True, f"{path.name} could not be read: {exc}")

        for node in ast.walk(tree):
            nodes += 1
            if nodes > WALK_NODE_LIMIT:
                return Verdict(True, "the dependency tree is larger than the walk follows")
            if not isinstance(node, ast.Call):
                continue
            name = _called_name(node)
            if name in _DYNAMIC_CALLS:
                return Verdict(True, f"{path.name} executes text at runtime via {name}")
            if name in _PROCESS_CALLS:
                if _names_git(node):
                    return Verdict(True, f"{path.name} runs git")
                return Verdict(True, f"{path.name} starts a process via {name}")

        for dependency in _local_imports(tree, path, repo_root):
            queue.append((dependency, depth + 1))

    return Verdict(False)


def can_run_git(script: Path, repo_root: Path) -> Verdict:
    """Whether this script, or anything local it reaches, can run git."""
    if not script.is_file():
        return Verdict(True, f"{script} is not a readable file")
    if script.suffix.lower() == ".py":
        return _inspect_python(script, repo_root)
    return Verdict(True, f"{script.suffix or 'this'} is not a language this walk parses")
