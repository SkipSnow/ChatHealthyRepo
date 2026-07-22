# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Rule-006 v2 — funnel classifier for PreToolUse Bash/PowerShell calls.

Five-stage funnel:

  1. Local allowlist. Regex allowlist of shell verbs that structurally cannot
     reach remote state (ls, cat, grep, git status/log/diff, rm/mv/cp on
     local paths, shell redirection, etc.). Match => allow, no LLM.

  2. Code-path walk. For anything else, follow every project script the
     command references (Python .py, PowerShell .ps1, shell .sh), recursively
     following imports of project modules, subprocess.run/call/Popen args,
     os.system/os.popen, source/dot-source of nested scripts. Bounded by
     max-nodes and visited-set. Unresolvable dynamic references (exec/eval of
     a runtime string, importlib on a computed name) => escalate.

  3. LLM classification. Assemble (command + wrapper info + all reachable
     files) into a payload. Ask Gemini 2.5 Flash Lite for one of three
     verdicts: allow / gate_approve / reject, plus a <=70-word explanation
     of what remote state is (or would be) mutated and by which line.

  4. Outcome dispatch:
        allow        -> exit 0 silently, tool call proceeds.
        gate_approve -> browser popup with command + wrapper explanation +
                        classifier explanation; operator types APPROVE or
                        REJECT. Approve => allow. Reject/timeout => deny.
        reject       -> deny IMMEDIATELY, no operator prompt; fire-and-forget
                        browser notification so the operator is still
                        informed about the attempt.

  5. Oneoff wrapper. Claude's only legal path to a non-chain remote mutation
     is `python architecture/EngineeringRuleEnforcement/code/oneoff.py
     --explanation "<why in <=70 words>" -- <actual command>`. The classifier
     recognises the wrapper, reads Claude's explanation, and verifies the
     wrapped command actually does what the explanation claims. Unwrapped
     remote-mutating commands that aren't one of the four legal chain
     surfaces (git commit, promote_chathealthy.py, build_chathealthy.py,
     deploy_chathealthy.py) => reject.

Fail-closed on: LLM error/timeout after retries, GEMINI_API_KEY missing,
unresolvable dynamic-code pattern, walker exceeds max-nodes budget.

Wiring: direct entry in .claude/settings.json under PreToolUse (Rule-064
pattern). Not routed through chathealthy_enforcement_manager because
PreToolUse needs a specific deny-JSON stdout format the manager does not
preserve.
"""

from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import secrets
import shlex
import random
import socket
import socketserver
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse


# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────
_MODEL = "gemini-2.5-flash-lite"
_GEMINI_ENDPOINT = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    f"{_MODEL}:generateContent"
)
_LLM_TIMEOUT_S = 75
_LLM_RETRIES = 6
_LLM_BACKOFF_INITIAL_S = 2.0
_LLM_BACKOFF_JITTER_S = 0.6
# When retries exhaust or the LLM is genuinely unreachable, escalate to human
# via gate_approve rather than fail-closed reject. This preserves the "Python
# programs always go through the LLM" rule while ensuring a transient Gemini
# outage never silently blocks legitimate work.
_LLM_UNAVAILABLE_VERDICT = "gate_approve"
_APPROVE_TIMEOUT_S = 600
_MAX_FILE_BYTES = 400_000
_MAX_TOTAL_BYTES = 1_500_000
_WALK_MAX_NODES = 60
_WALK_MAX_DEPTH = 6

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _PROJECT_ROOT / "Code" / ".env"
_AUDIT_DIR = (
    _PROJECT_ROOT
    / "architecture"
    / "EngineeringRuleEnforcement"
    / "ArchitectureDesignAndAuditDocs"
)
_AUDIT_LOG_PATH = _AUDIT_DIR / "llm_classify_shell_invocation.log"
_CACHE_PATH = _AUDIT_DIR / "llm_classify_shell_invocation.cache.json"

_ONEOFF_SCRIPT_REL = (
    "architecture/EngineeringRuleEnforcement/code/oneoff.py"
)

_SHELL_TOOLS = {"Bash", "PowerShell"}


# ─────────────────────────────────────────────────────────────────────────────
# .env loader — no external deps
# ─────────────────────────────────────────────────────────────────────────────
def _load_env_var(name: str) -> str | None:
    if not _ENV_FILE.exists():
        return None
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k.strip() == name:
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ('"', "'"):
                    v = v[1:-1]
                return v
    except OSError:
        return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — local allowlist (fast, no LLM)
# ─────────────────────────────────────────────────────────────────────────────
# Shell verbs whose observable effect is CONFINED to the local filesystem
# (this workstation, this git tree, this process). These skip the LLM.
#
# Rule: only pure verbs that cannot spawn a remote request under any argument
# shape. Anything that could exec a script (`python foo.py`, `pytest`,
# `npm run`) or reach a network endpoint (`curl`, `wget`, `az`, `docker`)
# must NOT be here — those go through Stages 2+3.
_LOCAL_ALLOWLIST = [
    # File / directory read
    r"^\s*ls(\s|$)",
    r"^\s*ll(\s|$)",
    r"^\s*cat\s",
    r"^\s*bat\s",
    r"^\s*head\s",
    r"^\s*tail\s",
    r"^\s*wc\s",
    r"^\s*file\s",
    r"^\s*stat\s",
    r"^\s*du(\s|$)",
    r"^\s*df(\s|$)",
    r"^\s*tree(\s|$)",
    r"^\s*find\s(?!.*-(delete|exec|execdir|ok|okdir)\b)",
    r"^\s*grep\s",
    r"^\s*egrep\s",
    r"^\s*fgrep\s",
    r"^\s*rg\s",
    r"^\s*ack\s",
    r"^\s*awk\s",
    r"^\s*sed\s(?!.*-i\b)",  # sed without -i is read-only
    r"^\s*sort\s",
    r"^\s*uniq\s",
    r"^\s*cut\s",
    r"^\s*tr\s",
    r"^\s*jq\s",
    r"^\s*yq\s",
    r"^\s*xmllint\s",
    r"^\s*diff\s",
    r"^\s*cmp\s",
    r"^\s*hexdump\s",
    r"^\s*xxd\s",
    r"^\s*od\s",
    r"^\s*strings\s",
    r"^\s*less\s",
    r"^\s*more\s",
    # Local filesystem writes / edits
    r"^\s*rm\s(?!.*\s(/|~|-r?f?\s+/))",  # rm on local; don't allow rm on root paths
    r"^\s*mv\s",
    r"^\s*cp\s",
    r"^\s*ln\s",
    r"^\s*mkdir\s",
    r"^\s*rmdir\s",
    r"^\s*touch\s",
    r"^\s*chmod\s",
    r"^\s*chown\s",
    r"^\s*tar\s",
    r"^\s*gzip\s",
    r"^\s*gunzip\s",
    r"^\s*zip\s(?!.*http)",
    r"^\s*unzip\s",
    # Info / metadata
    r"^\s*pwd(\s|$)",
    r"^\s*whoami(\s|$)",
    r"^\s*hostname(\s|$)",
    r"^\s*id(\s|$)",
    r"^\s*date(\s|$)",
    r"^\s*uptime(\s|$)",
    r"^\s*uname(\s|$)",
    r"^\s*which\s",
    r"^\s*whereis\s",
    r"^\s*type\s",
    r"^\s*env(\s|$)",
    r"^\s*printenv(\s|$)",
    r"^\s*history(\s|$)",
    # Shell echo / print
    r"^\s*echo(\s|$)",
    r"^\s*printf\s",
    r"^\s*true(\s|$)",
    r"^\s*false(\s|$)",
    r"^\s*yes(\s|$)",
    # Git push — Rule-065 makes push-after-authorized-commit implicit in the
    # commit authorization; the commit has already cleared the pre-commit gate,
    # so push carries no additional governance need. Force-push forms
    # (--force, -f, --force-with-lease, --force-if-includes) and delete-remote
    # ref syntax (`origin :branch`) are excluded — those are destructive and
    # require explicit operator approval via oneoff.py.
    r"^\s*git\s+push\b(?!.*(?:--force\b|--force-with-lease\b|--force-if-includes\b|(?:^|\s)-f\b|\s:\s|\s:$))",
    # Git READ-only verbs (other write verbs like commit/fetch/pull are NOT here)
    r"^\s*git\s+status(\s|$)",
    r"^\s*git\s+log(\s|$)",
    r"^\s*git\s+diff(\s|$)",
    r"^\s*git\s+show(\s|$)",
    r"^\s*git\s+branch(\s|$|\s+--?list)",
    r"^\s*git\s+rev-parse(\s|$)",
    r"^\s*git\s+rev-list(\s|$)",
    r"^\s*git\s+blame(\s|$)",
    r"^\s*git\s+describe(\s|$)",
    r"^\s*git\s+ls-files(\s|$)",
    r"^\s*git\s+ls-tree(\s|$)",
    r"^\s*git\s+config\s+--get(\s|$)",
    r"^\s*git\s+config\s+--list(\s|$)",
    r"^\s*git\s+remote\s+(-v|show)(\s|$)",
    r"^\s*git\s+stash\s+(list|show)(\s|$)",
    r"^\s*git\s+worktree\s+list(\s|$)",
    r"^\s*git\s+shortlog(\s|$)",
    # Remote READS are ALWAYS permitted. Reads never mutate; the operator
    # never wants to gate them. Patterns match verbs that only fetch state.
    r"^\s*az\s+\S+\s+(show|list|list-.+|get|get-.+|export|search|query)(\s|$)",
    r"^\s*az\s+\S+\s+\S+\s+(show|list|list-.+|get|get-.+|export|search|query)(\s|$)",
    r"^\s*az\s+account\s+(show|list|get-access-token|list-locations)(\s|$)",
    r"^\s*az\s+version(\s|$)",
    r"^\s*az\s+rest\s+--method\s+get\b",
    r"^\s*gh\s+(api\s+(?!(-X|--method)\s*(POST|PUT|PATCH|DELETE))|(pr|issue|run|release|repo|workflow)\s+(list|view|status)|auth\s+status)",
    r"^\s*docker\s+(ps|images|image\s+ls|inspect|logs|pull\s+)",
    r"^\s*git\s+fetch\b",
    r"^\s*git\s+ls-remote\b",
    r"^\s*nslookup\s",
    r"^\s*dig\s",
    r"^\s*host\s",
    r"^\s*ping\s",
    r"^\s*curl\s+(-[a-zA-Z]*[LsSk]+[a-zA-Z]*\s+)*(https?://)",  # curl GET (default method)
    r"^\s*wget\s+(-[a-zA-Z]+\s+)*(https?://)",  # wget GET (default)
    # Windows / PowerShell read-only equivalents
    r"^\s*Get-ChildItem(\s|$)",
    r"^\s*Get-Content(\s|$)",
    r"^\s*Get-Item(\s|$)",
    r"^\s*Get-Location(\s|$)",
    r"^\s*Test-Path(\s|$)",
    r"^\s*Select-String(\s|$)",
    r"^\s*Measure-Object(\s|$)",
    r"^\s*Where-Object(\s|$)",
    r"^\s*ForEach-Object(\s|$)",
    r"^\s*Get-Date(\s|$)",
    r"^\s*Get-Command(\s|$)",
    r"^\s*Get-Process(\s|$)",
]

_LOCAL_ALLOWLIST_RE = [re.compile(p, re.IGNORECASE) for p in _LOCAL_ALLOWLIST]


def _is_local_allowlisted(command: str) -> bool:
    """True iff every effect of `command` is confined to the local filesystem.
    Splits on shell separators; every segment must match the allowlist."""
    # Split on shell separators, keep only non-empty segments.
    segments = re.split(r"(?:&&|\|\||;|\||\n)", command)
    segments = [s.strip() for s in segments if s.strip()]
    if not segments:
        return False
    for seg in segments:
        if not any(rgx.search(seg) for rgx in _LOCAL_ALLOWLIST_RE):
            return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — recursive code-path walker
# ─────────────────────────────────────────────────────────────────────────────
_SCRIPT_HEAD_RE = re.compile(
    r"\b(?:python[3]?|py|bash|sh|pwsh|powershell|node|ruby|perl|npx)\b"
    r"(?:\s+-\S+(?:\s+[^\s|;&<>]+)?)*"
    r"\s+"
    r"([A-Za-z]:[/\\][^\s|;&<>]+\.(?:py|sh|ps1|bash|js|ts|rb|pl)|"
    r"[A-Za-z0-9_./\\-]+\.(?:py|sh|ps1|bash|js|ts|rb|pl))",
    re.IGNORECASE,
)


def _read_file_bounded(p: Path) -> tuple[str | None, str | None]:
    """Return (content, error). Content is capped at _MAX_FILE_BYTES."""
    try:
        size = p.stat().st_size
    except OSError as exc:
        return None, f"stat failed: {exc}"
    if size > _MAX_FILE_BYTES:
        return None, f"file exceeds {_MAX_FILE_BYTES} bytes ({size})"
    try:
        return p.read_text(encoding="utf-8", errors="replace"), None
    except OSError as exc:
        return None, f"read failed: {exc}"


def _resolve_project_path(candidate: str) -> Path | None:
    """Return an absolute Path inside _PROJECT_ROOT, or None."""
    if not candidate:
        return None
    p = Path(candidate)
    if not p.is_absolute():
        p = (_PROJECT_ROOT / candidate).resolve()
    else:
        try:
            p = p.resolve()
        except OSError:
            return None
    try:
        p.relative_to(_PROJECT_ROOT)
    except ValueError:
        return None
    if not p.is_file():
        return None
    return p


def _module_to_path(module_dotted: str, from_dir: Path | None) -> Path | None:
    """Resolve a Python dotted module name to a project file. Only follows
    project-local modules; std-lib and third-party wheels are skipped by
    returning None."""
    if not module_dotted:
        return None
    parts = module_dotted.split(".")
    # First try relative to the importing file's directory (Python's normal
    # sys.path[0] semantic for scripts).
    search_roots: list[Path] = []
    if from_dir is not None:
        search_roots.append(from_dir)
    # Then all top-level project directories that could be added to sys.path
    # by our runners (pipeline/Code, FrontEndApplicationLib/src, etc.).
    search_roots.extend([
        _PROJECT_ROOT,
        _PROJECT_ROOT / "pipeline" / "Code",
        _PROJECT_ROOT / "FrontEndApplicationLib" / "src",
        _PROJECT_ROOT / "sharedServices" / "Code",
        _PROJECT_ROOT / "FindCare",
        _PROJECT_ROOT / "evaluateCare" / "Code",
        _PROJECT_ROOT / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement",
        _PROJECT_ROOT / "architecture" / "EngineeringRuleEnforcement" / "code",
    ])
    for root in search_roots:
        # a.b.c -> a/b/c.py
        cand = root.joinpath(*parts).with_suffix(".py")
        p = _resolve_project_path(str(cand))
        if p is not None:
            return p
        # a.b.c -> a/b/c/__init__.py
        cand2 = root.joinpath(*parts, "__init__.py")
        p = _resolve_project_path(str(cand2))
        if p is not None:
            return p
    return None


def _extract_python_refs(source: str, from_file: Path) -> tuple[list[str], list[str]]:
    """Return (referenced_paths, dynamic_evidence). Referenced paths are
    project-relative file paths. Dynamic_evidence names call-sites that
    prevent static resolution (exec/eval/compile/getattr-based dispatch)."""
    refs: list[str] = []
    dynamic: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        return refs, [f"ast.parse failed at line {exc.lineno}: {exc.msg}"]

    from_dir = from_file.parent

    for node in ast.walk(tree):
        # imports
        if isinstance(node, ast.Import):
            for alias in node.names:
                p = _module_to_path(alias.name, from_dir)
                if p is not None:
                    refs.append(str(p))
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level and node.level > 0:
                # relative import: assemble against from_dir
                base = from_dir
                for _ in range(node.level - 1):
                    base = base.parent
                dotted = ".".join(base.relative_to(_PROJECT_ROOT).parts + ((module,) if module else ()))
                p = _module_to_path(dotted, None)
                if p is not None:
                    refs.append(str(p))
                for name in node.names:
                    dotted_sub = f"{dotted}.{name.name}" if dotted else name.name
                    p = _module_to_path(dotted_sub, None)
                    if p is not None:
                        refs.append(str(p))
            else:
                p = _module_to_path(module, from_dir)
                if p is not None:
                    refs.append(str(p))
                for name in node.names:
                    dotted_sub = f"{module}.{name.name}" if module else name.name
                    p = _module_to_path(dotted_sub, from_dir)
                    if p is not None:
                        refs.append(str(p))
        # subprocess.run/call/Popen/check_output/check_call
        elif isinstance(node, ast.Call):
            fname = _call_qualified_name(node.func)
            if fname in {
                "subprocess.run", "subprocess.call", "subprocess.Popen",
                "subprocess.check_output", "subprocess.check_call",
                "os.system", "os.popen", "runpy.run_path", "runpy.run_module",
                "importlib.import_module", "__import__",
            }:
                arg_paths = _extract_call_string_args(node)
                for a in arg_paths:
                    # If the arg looks like a script path, resolve it.
                    if any(a.endswith(ext) for ext in (".py", ".sh", ".ps1", ".bash", ".js", ".ts")):
                        p = _resolve_project_path(a)
                        if p is not None:
                            refs.append(str(p))
                        else:
                            dynamic.append(f"{fname}(...) references {a!r} — not in project tree")
                    elif "." in a and "/" not in a and "\\" not in a:
                        # Looks like a dotted module name
                        p = _module_to_path(a, from_dir)
                        if p is not None:
                            refs.append(str(p))
                if not arg_paths:
                    dynamic.append(f"{fname}(...) — dynamic args, cannot resolve statically")
            elif fname in {"exec", "eval", "compile"}:
                dynamic.append(f"{fname}(...) — dynamic code execution")

    return sorted(set(refs)), dynamic


def _call_qualified_name(node: ast.AST) -> str:
    """Return 'module.attr' or 'name' for a call target; empty on unknown."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parts: list[str] = []
        cur: ast.AST = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        if isinstance(cur, ast.Name):
            parts.append(cur.id)
            return ".".join(reversed(parts))
    return ""


def _extract_call_string_args(call: ast.Call) -> list[str]:
    """Collect any string literals that appear directly as args (positional
    or in a list literal). Returns the raw string values."""
    out: list[str] = []
    for a in call.args:
        if isinstance(a, ast.Constant) and isinstance(a.value, str):
            out.append(a.value)
        elif isinstance(a, ast.List):
            for elt in a.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value)
        elif isinstance(a, ast.Tuple):
            for elt in a.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    out.append(elt.value)
    return out


_SHELL_SOURCE_RE = re.compile(
    r"(?:^|\s|;|&|\|)(?:source|\.)\s+([^\s;&|<>]+)",
    re.MULTILINE,
)
_SHELL_INVOKE_RE = re.compile(
    r"(?:^|\s|;|&|\||`|\$\()"
    r"(?:python[3]?|py|bash|sh|pwsh|powershell|node|ruby|perl)\s+"
    r"([^\s|;&<>`)]+\.(?:py|sh|ps1|bash|js|ts|rb|pl))",
    re.IGNORECASE,
)
_PWSH_DOTSOURCE_RE = re.compile(
    r"(?:^|\s|;|&|\|)\.\s+([^\s;&|]+\.ps1)",
    re.MULTILINE | re.IGNORECASE,
)


def _extract_shell_refs(source: str) -> list[str]:
    refs: set[str] = set()
    for m in _SHELL_SOURCE_RE.finditer(source):
        refs.add(m.group(1))
    for m in _SHELL_INVOKE_RE.finditer(source):
        refs.add(m.group(1))
    for m in _PWSH_DOTSOURCE_RE.finditer(source):
        refs.add(m.group(1))
    return sorted(refs)


def _walk_command_closure(command: str) -> tuple[list[dict], list[str], list[str]]:
    """Return (files, errors, dynamic_evidence).

    files: list of {path, content, sha256} for every project-local file
    reachable from the command (transitive imports, subprocess targets,
    shell source, etc.).

    errors: fail-closed conditions — file unreadable, size cap exceeded,
    walker budget exhausted.

    dynamic_evidence: static-analysis limitations (exec/eval, dynamic
    subprocess args). The LLM sees these and factors them in; they do NOT
    auto-fail-closed, but they raise the classifier's suspicion.
    """
    files: list[dict] = []
    errors: list[str] = []
    dynamic: list[str] = []
    visited: set[str] = set()

    # Seed: everything the top-level command directly references.
    seeds: list[tuple[str, int]] = []
    for m in _SCRIPT_HEAD_RE.finditer(command):
        seeds.append((m.group(1), 0))

    total_bytes = 0
    while seeds:
        if len(visited) >= _WALK_MAX_NODES:
            errors.append(f"walker exceeded max-nodes budget ({_WALK_MAX_NODES})")
            break
        raw_path, depth = seeds.pop(0)
        if depth > _WALK_MAX_DEPTH:
            errors.append(f"walker exceeded max-depth ({_WALK_MAX_DEPTH}) at {raw_path}")
            continue
        p = _resolve_project_path(raw_path)
        if p is None:
            errors.append(f"referenced file not in project tree: {raw_path!r}")
            continue
        key = str(p)
        if key in visited:
            continue
        visited.add(key)
        content, err = _read_file_bounded(p)
        if err:
            errors.append(f"{p}: {err}")
            continue
        total_bytes += len(content or "")
        if total_bytes > _MAX_TOTAL_BYTES:
            errors.append(
                f"walker exceeded total-bytes budget ({_MAX_TOTAL_BYTES})"
            )
            break
        digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()[:16]
        files.append({
            "path": str(p.relative_to(_PROJECT_ROOT)).replace("\\", "/"),
            "content": content or "",
            "sha256": digest,
        })
        # Extract refs based on file type
        suffix = p.suffix.lower()
        if suffix == ".py":
            refs, dyn = _extract_python_refs(content or "", p)
            dynamic.extend(dyn)
            for r in refs:
                seeds.append((r, depth + 1))
        elif suffix in (".sh", ".bash", ".ps1"):
            for r in _extract_shell_refs(content or ""):
                seeds.append((r, depth + 1))

    return files, errors, dynamic


# ─────────────────────────────────────────────────────────────────────────────
# Oneoff wrapper detection
# ─────────────────────────────────────────────────────────────────────────────
_ONEOFF_INVOKE_RE = re.compile(
    r"\b(?:python[3]?|py|\.venv[/\\]Scripts[/\\]python(?:\.exe)?)\b"
    r"\s+[^\s|;&]*oneoff\.py"
    r"(?P<tail>[^\r\n]*)",
    re.IGNORECASE,
)


def _extract_oneoff(command: str) -> dict | None:
    """If the command surface invokes oneoff.py, return {explanation, inner}.
    Otherwise None."""
    m = _ONEOFF_INVOKE_RE.search(command)
    if not m:
        return None
    tail = m.group("tail")
    # Parse tail with shlex to pull --explanation and everything after `--`.
    try:
        tokens = shlex.split(tail, posix=(os.name != "nt"))
    except ValueError:
        return {"explanation": "", "inner": "", "parse_error": True}
    explanation = ""
    inner_tokens: list[str] = []
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t == "--":
            inner_tokens = tokens[i + 1:]
            break
        if t == "--explanation" and i + 1 < len(tokens):
            explanation = tokens[i + 1]
            i += 2
            continue
        if t.startswith("--explanation="):
            explanation = t.split("=", 1)[1]
            i += 1
            continue
        i += 1
    inner_cmd = " ".join(shlex.quote(x) for x in inner_tokens) if inner_tokens else ""
    return {"explanation": explanation.strip(), "inner": inner_cmd, "parse_error": False}


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────
def _payload_hash(command: str, files: list[dict], wrapper: dict | None) -> str:
    h = hashlib.sha256()
    h.update(command.encode("utf-8", errors="replace"))
    for f in files:
        h.update(f["path"].encode("utf-8"))
        h.update(f["sha256"].encode("utf-8"))
    if wrapper:
        h.update(b"|wrapper|")
        h.update((wrapper.get("explanation") or "").encode("utf-8"))
        h.update((wrapper.get("inner") or "").encode("utf-8"))
    return h.hexdigest()


def _cache_load() -> dict:
    try:
        with _CACHE_PATH.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _cache_get(key: str) -> tuple[str, str] | None:
    data = _cache_load()
    entry = data.get(key)
    if not entry:
        return None
    return entry.get("classification", ""), entry.get("reason", "")


def _cache_put(key: str, classification: str, reason: str) -> None:
    try:
        _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        data = _cache_load()
        data[key] = {"classification": classification, "reason": reason,
                     "ts": datetime.now(timezone.utc).isoformat()}
        with _CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, sort_keys=True)
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# LLM prompt + call
# ─────────────────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are Rule-006. One question only: does this
command mutate state on a REMOTE node? Answer with a JSON verdict.

Return STRICT JSON: {"classification": "allow|gate_approve|reject", "reason": "<=70 words>"}

DECISION TREE (evaluate top-down, first match wins)

1) Is the outer program one of: git commit, promote_chathealthy.py,
   build_chathealthy.py, deploy_chathealthy.py?
      YES -> ALLOW. (These are the four governed chain surfaces with
      their own gates. Return allow immediately. Do NOT require a
      wrapper. Do NOT analyze internals.)

2) Do the command AND every code path the walker reaches from it fail
   to mutate ANY remote state?
      YES -> ALLOW. (Local file/git/tmp work: allow. Remote READS:
      allow. Reads include HTTP GET, `az ... show/list`, `az rest
      --method get`, `az keyvault secret show`, `az storage blob
      download`, `az automation job show`, `az account show`, pymongo
      find/aggregate/count/find_one, `gh api` default GET, `docker
      pull`, `git fetch`, `git ls-remote`, DNS lookups, MI/auth token
      reads. Auth-carrying does NOT make a read a write. Filenames do
      NOT matter — `test_x.py` can still POST; read the code.)

3) Does SOME code path mutate remote state AND is the command wrapped
   by `python .../oneoff.py --explanation "<text>" -- <inner>` AND is
   the explanation honest about the mutation?
      YES -> GATE_APPROVE. (Operator gets popup, decides.)

4) Otherwise (mutation with no wrapper, or wrapper with empty/dishonest
   explanation) -> REJECT.

REMOTE MUTATION = HTTP POST/PUT/DELETE/PATCH to non-local host,
`az ... create/update/delete/restart/start/stop/set/replace`,
`az rest --method put/post/delete/patch`, `az keyvault secret
set/delete/purge`, `az storage blob upload/delete`, pymongo
insert/update/delete/replace/drop, `gh api --method POST/PUT/PATCH/DELETE`,
`docker push`, `git push` (anything except fetch/ls-remote), `wrangler
deploy`, and equivalent remote writes on other providers.

CRITICAL: A wrapper is NEVER required for allow verdicts. The wrapper
only matters when the command actually mutates remote state. If your
own reasoning says "no mutation detected" then the verdict is ALLOW —
do not then reject because of missing wrapper. Missing wrapper is only
a reject reason when mutation IS present.

UNKNOWNS: Dynamic code (exec/eval/importlib on runtime strings) or
walker errors mean you cannot verify. If context suggests mutation,
prefer reject (or gate_approve if wrapped). Do not fabricate a call
graph you did not see.

REASON (<=70 plain-English words): Name the classification driver
("chain surface: <script>", "no mutation: <what code does>", "mutation
at <file:line>: <what>", or "wrapper missing/dishonest for <mutation>").
Shown to operator on popup or notification.
"""


def _classify_via_llm(api_key: str, payload_text: str) -> tuple[str, str, str | None]:
    """Return (classification, reason, error). Error is None on success.

    Reliability policy:
      * Retry _LLM_RETRIES times on any 429 / 5xx OR any network/timeout error.
      * Exponential backoff starting at _LLM_BACKOFF_INITIAL_S, plus random
        jitter to avoid thundering-herd retries.
      * On 4xx errors other than 429 (misconfig, invalid API key, quota),
        surface a specific reason immediately — no retries, those are
        deterministic API rejections that won't succeed on retry.
      * When retries exhaust OR a genuine LLM outage is detected, return
        `_LLM_UNAVAILABLE_VERDICT` (gate_approve) with the actual last error
        embedded in the reason — this asks the operator to approve instead
        of silently blocking work.
    """
    combined = _SYSTEM_PROMPT + "\n\n---\n\n" + payload_text
    body = {
        "contents": [{"parts": [{"text": combined}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
        },
    }
    url = f"{_GEMINI_ENDPOINT}?key={api_key}"
    delay = _LLM_BACKOFF_INITIAL_S
    last_err: str | None = None

    for attempt in range(1, _LLM_RETRIES + 1):
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        raw: str | None = None
        try:
            with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code} {exc.reason} (attempt {attempt}/{_LLM_RETRIES})"
            # 4xx non-429 = deterministic rejection, don't retry
            if exc.code != 429 and 400 <= exc.code < 500:
                reason = (
                    f"LLM classifier returned HTTP {exc.code} {exc.reason}. "
                    f"This is a deterministic API rejection (likely misconfig, "
                    f"invalid API key, or quota exhausted). Requires operator "
                    f"approval to proceed."
                )
                return _LLM_UNAVAILABLE_VERDICT, reason, last_err
            # 429 or 5xx — transient, retry with backoff + jitter
            if attempt < _LLM_RETRIES:
                jitter = random.uniform(0, _LLM_BACKOFF_JITTER_S)
                time.sleep(delay + jitter)
                delay *= 2
                continue
        except (urllib.error.URLError, socket.timeout, TimeoutError,
                ConnectionError, OSError) as exc:
            last_err = f"network: {type(exc).__name__}: {exc} (attempt {attempt}/{_LLM_RETRIES})"
            if attempt < _LLM_RETRIES:
                jitter = random.uniform(0, _LLM_BACKOFF_JITTER_S)
                time.sleep(delay + jitter)
                delay *= 2
                continue
        except Exception as exc:  # noqa: BLE001
            last_err = f"unexpected: {type(exc).__name__}: {exc}"
            if attempt < _LLM_RETRIES:
                jitter = random.uniform(0, _LLM_BACKOFF_JITTER_S)
                time.sleep(delay + jitter)
                delay *= 2
                continue

        if raw is None:
            # Retry loop exhausted for this attempt slot; go to next iteration
            continue

        # Parse the LLM response
        try:
            wrapper = json.loads(raw)
            text = wrapper["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            cls = parsed.get("classification", "")
            rsn = parsed.get("reason", "")
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            last_err = f"unparsable response: {exc} :: {raw[:400]}"
            if attempt < _LLM_RETRIES:
                jitter = random.uniform(0, _LLM_BACKOFF_JITTER_S)
                time.sleep(delay + jitter)
                delay *= 2
                continue
            reason = (
                f"LLM classifier returned an unparsable response after "
                f"{_LLM_RETRIES} attempts. Requires operator approval to proceed. "
                f"Last error: {last_err}"
            )
            return _LLM_UNAVAILABLE_VERDICT, reason, last_err

        if cls not in ("allow", "gate_approve", "reject"):
            return "reject", f"LLM returned unknown classification {cls!r}", None
        return cls, rsn or "no reason given", None

    # Retries exhausted — fall back to gate_approve so operator can decide
    reason = (
        f"LLM classifier unreachable after {_LLM_RETRIES} attempts "
        f"({int(_LLM_BACKOFF_INITIAL_S * (2 ** _LLM_RETRIES))}s total budget). "
        f"Requires operator approval to proceed. Last error: {last_err or 'unknown'}"
    )
    return _LLM_UNAVAILABLE_VERDICT, reason, last_err


# ─────────────────────────────────────────────────────────────────────────────
# Browser notification (fire-and-forget on reject)
# ─────────────────────────────────────────────────────────────────────────────
def _notify_browser(tool_name: str, command: str, code_fingerprints: list[str],
                    reason: str, verdict: str, extra: str = "") -> None:
    html = (
        "<!doctype html><html><head><meta charset=utf-8>"
        f"<title>Rule-006 {verdict.upper()}</title>"
        "<style>body{font-family:system-ui,sans-serif;padding:40px;"
        "background:#7f1d1d;color:#fff;max-width:900px;margin:auto}"
        "h1{font-size:24px}pre{background:#00000044;padding:16px;"
        "border-radius:6px;white-space:pre-wrap;overflow-x:auto}"
        ".hash{font-family:monospace;color:#fca5a5}</style></head>"
        "<body>"
        f"<h1>Rule-006 {verdict.upper()}</h1>"
        f"<p><b>Tool:</b> {tool_name}</p>"
        f"<p><b>Timestamp:</b> {datetime.now(timezone.utc).isoformat()}</p>"
        f"<p><b>Reason (classifier, &lt;=70 words):</b> {reason}</p>"
        + (f"<p>{extra}</p>" if extra else "")
        + f"<p><b>Code fingerprints:</b> "
        + (", ".join(f'<span class="hash">{h}</span>' for h in code_fingerprints) or "none")
        + "</p>"
        "<h2>Command</h2>"
        f"<pre>{command.replace('<', '&lt;').replace('>', '&gt;')}</pre>"
        "</body></html>"
    )
    try:
        fd, path = tempfile.mkstemp(suffix="_rule006.html")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(html)
        webbrowser.open_new_tab(f"file:///{path.replace(os.sep, '/')}")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# APPROVE prompt for gate_approve — inline TTY or browser page
# ─────────────────────────────────────────────────────────────────────────────
_AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")


def _is_real_interactive_shell() -> bool:
    for marker in _AGENT_MARKERS:
        if os.environ.get(marker):
            return False
    return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()


def _prompt_approve_inline(command: str, reason: str, wrapper: dict | None) -> bool:
    sys.stderr.write("\nRule-006 gate_approve\n")
    if wrapper:
        sys.stderr.write(f"Claude's explanation: {wrapper.get('explanation') or '(empty)'}\n")
    sys.stderr.write(f"Classifier reason: {reason}\n")
    sys.stderr.write(f"Command: {command}\nApprove? (type APPROVE) ")
    sys.stderr.flush()
    try:
        reply = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        return False
    return reply.strip() == "APPROVE"


def _prompt_approve_browser(tool_name: str, command: str, reason: str,
                            code_fingerprints: list[str], wrapper: dict | None) -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    except Exception:  # noqa: BLE001
        return False
    verdict: dict[str, str | None] = {"value": None}
    token = secrets.token_urlsafe(8)
    esc_cmd = command.replace("<", "&lt;").replace(">", "&gt;")
    wrapper_block = ""
    if wrapper:
        exp = (wrapper.get("explanation") or "(empty)").replace("<", "&lt;").replace(">", "&gt;")
        wrapper_block = (
            "<h2>Claude's explanation (oneoff wrapper)</h2>"
            f"<pre>{exp}</pre>"
        )
    page = (
        "<!doctype html><html><head><meta charset=utf-8>"
        "<title>Rule-006 gate_approve</title>"
        "<style>body{font-family:system-ui,sans-serif;padding:40px;"
        "background:#78350f;color:#fff;max-width:900px;margin:auto}"
        "h1{font-size:24px}pre{background:#00000044;padding:16px;"
        "border-radius:6px;white-space:pre-wrap;overflow-x:auto}"
        "button{font-size:18px;padding:14px 32px;margin:8px;border:none;"
        "border-radius:6px;cursor:pointer;font-weight:600}"
        ".approve{background:#0b9a94;color:#fff}"
        ".reject{background:#dc2626;color:#fff}</style></head>"
        "<body>"
        f"<h1>Approve {tool_name} invocation?</h1>"
        f"<p><b>Classifier reason:</b> {reason}</p>"
        + wrapper_block
        + f"<p><b>Code fingerprints:</b> "
        + (", ".join(f"<code>{h}</code>" for h in code_fingerprints) or "none")
        + "</p>"
        f"<h2>Command</h2><pre>{esc_cmd}</pre>"
        f"<form id=f method=POST action=/decide>"
        f"<button class=approve name=verdict value=approve type=submit>APPROVE</button>"
        f"<button class=reject name=verdict value=reject type=submit>REJECT</button>"
        f"<input type=hidden name=token value=\"{token}\"></form>"
        "<script>document.getElementById('f').addEventListener('submit',async function(e){"
        "e.preventDefault();var fd=new FormData(e.target);"
        "if(e.submitter&&e.submitter.name)fd.set(e.submitter.name,e.submitter.value);"
        "try{var r=await fetch('/decide',{method:'POST',body:new URLSearchParams(fd)});"
        "if(r.ok){window.close();document.body.innerHTML='<h1>Recorded.</h1>';}"
        "else{document.body.innerHTML='<h1>Error '+r.status+'</h1>';}}"
        "catch(err){document.body.innerHTML='<h1>Network error</h1>';}"
        "});</script></body></html>"
    )
    ack = "<!doctype html><h1 style=\"font-family:system-ui;padding:40px\">Recorded.</h1>"

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return
        def _send(self, code, body):
            data = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):  # noqa: N802
            if urlparse(self.path).path in ("/", "/prompt"):
                self._send(200, page)
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
                self._send(200, ack)
            else:
                self._send(400, "<h1>Bad verdict</h1>")

    try:
        server = socketserver.TCPServer(("127.0.0.1", port), _Handler, bind_and_activate=True)
    except Exception:  # noqa: BLE001
        return False
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        webbrowser.open_new(f"http://127.0.0.1:{port}/prompt")
    except Exception:  # noqa: BLE001
        pass
    sys.stderr.write(f"\nRule-006 approval requested at: http://127.0.0.1:{port}/prompt\n")
    sys.stderr.flush()
    deadline = time.time() + _APPROVE_TIMEOUT_S
    try:
        while time.time() < deadline:
            if verdict["value"] is not None:
                break
            time.sleep(0.25)
    finally:
        try:
            server.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            server.server_close()
        except Exception:  # noqa: BLE001
            pass
    return verdict["value"] == "approve"


# ─────────────────────────────────────────────────────────────────────────────
# Audit log + deny emitter
# ─────────────────────────────────────────────────────────────────────────────
def _audit(entry: dict) -> None:
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


def _emit_deny(reason: str) -> None:
    response = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        },
        "systemMessage": f"Rule-006 blocked: {reason}",
    }
    print(json.dumps(response))


# ─────────────────────────────────────────────────────────────────────────────
# Deploy-from-git preflight (governance orthogonal to classification)
# ─────────────────────────────────────────────────────────────────────────────
_DEPLOY_ENV_NONLOCAL_RE = re.compile(
    r"(?:^|[\r\n]|(?<=[|;&])\s*)"
    r"python[3]?\s+"
    r"[^\s\n|;&]*(?:deploy|promote)_chathealthy\.py\b"
    r"[^|;&\n\r]{0,300}--env\s+(dev|qa|prod)\b",
    re.IGNORECASE,
)


def _git_state_ok_for_deploy() -> tuple[bool, str]:
    import subprocess as _sp
    try:
        r = _sp.run(["git", "status", "--porcelain"], cwd=str(_PROJECT_ROOT),
                    capture_output=True, text=True, timeout=15)
        if r.returncode != 0:
            return False, f"git status failed rc={r.returncode}: {r.stderr[:200]}"
        if r.stdout.strip():
            dirty_lines = r.stdout.strip().splitlines()
            return False, (
                f"working tree is dirty ({len(dirty_lines)} entries): "
                + "; ".join(dirty_lines[:6])
            )
        r = _sp.run(["git", "symbolic-ref", "--short", "HEAD"],
                    cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=10)
        if r.returncode != 0 or not r.stdout.strip():
            return False, "cannot resolve current branch (detached HEAD?)"
        branch = r.stdout.strip()
        local = _sp.run(["git", "rev-parse", "HEAD"], cwd=str(_PROJECT_ROOT),
                        capture_output=True, text=True, timeout=10)
        remote = _sp.run(["git", "ls-remote", "origin", f"refs/heads/{branch}"],
                         cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=30)
        if local.returncode != 0 or remote.returncode != 0:
            return False, "cannot compare HEAD to origin"
        local_sha = local.stdout.strip()
        remote_line = remote.stdout.strip().split("\t") if remote.stdout.strip() else []
        remote_sha = remote_line[0] if remote_line else ""
        if not remote_sha:
            return False, f"origin has no branch {branch!r}"
        if local_sha != remote_sha:
            return False, (
                f"HEAD ({local_sha[:12]}) differs from origin/{branch} "
                f"({remote_sha[:12]}); push before deploying"
            )
        return True, "clean tree + HEAD at origin"
    except _sp.TimeoutExpired:
        return False, "git check timed out"
    except Exception as exc:  # noqa: BLE001
        return False, f"git check errored: {exc}"


# ─────────────────────────────────────────────────────────────────────────────
# main
# ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception as exc:  # noqa: BLE001
        print(f"llm_classify_shell_invocation_worker: stdin parse error: {exc}", file=sys.stderr)
        return 0

    tool_name = payload.get("tool_name", "")
    if tool_name not in _SHELL_TOOLS:
        return 0

    command = (payload.get("tool_input", {}) or {}).get("command", "") or ""
    if not command.strip():
        return 0

    ts = datetime.now(timezone.utc).isoformat()

    # Deploy-from-git preflight (runs before classification; the LLM cannot
    # see git state). Only non-local deploys are subject to this check.
    if _DEPLOY_ENV_NONLOCAL_RE.search(command):
        ok, reason = _git_state_ok_for_deploy()
        if not ok:
            deny_reason = (
                f"Rule-006 deploy-from-git: {reason}. Non-local deploys MUST "
                f"come 100% from git. Commit + push before invoking deploy."
            )
            _audit({"ts": ts, "tool": tool_name, "command": command,
                    "verdict": "reject", "reason": deny_reason,
                    "stage": "deploy_from_git_preflight"})
            _notify_browser(tool_name, command, [], deny_reason, "reject")
            _emit_deny(deny_reason)
            return 0

    # STAGE 1 — local allowlist (no LLM)
    if _is_local_allowlisted(command):
        _audit({"ts": ts, "tool": tool_name, "command": command,
                "verdict": "allow", "reason": "local allowlist",
                "stage": "1_local_allowlist"})
        return 0

    # STAGE 2 — recursive code-path walk
    files, walk_errors, dynamic = _walk_command_closure(command)
    code_hashes = [f["sha256"] for f in files]

    # STAGE 5 — oneoff wrapper detection (recognized here so its payload is
    # part of the LLM's context)
    wrapper = _extract_oneoff(command)

    api_key = _load_env_var("GEMINI_API_KEY") or _load_env_var("GOOGLE_API_KEY")
    if not api_key:
        reason = "Rule-006 misconfigured: GEMINI_API_KEY not found in Code/.env"
        _audit({"ts": ts, "tool": tool_name, "command": command,
                "verdict": "reject", "reason": reason, "stage": "config"})
        _notify_browser(tool_name, command, code_hashes, reason, "reject")
        _emit_deny(reason)
        return 0

    # Cache
    cache_key = _payload_hash(command, files, wrapper)
    cached = _cache_get(cache_key)
    if cached:
        classification, reason = cached
        audit_entry = {
            "ts": ts, "tool": tool_name, "command": command,
            "verdict": classification, "reason": reason,
            "cache": "hit", "code_fingerprints": code_hashes,
            "walk_errors": walk_errors, "dynamic_evidence": dynamic,
            "wrapped": bool(wrapper),
        }
        return _dispatch_outcome(classification, reason, tool_name, command,
                                 code_hashes, wrapper, audit_entry)

    # STAGE 3 — LLM classifier
    parts: list[str] = [
        f"TOOL: {tool_name}",
        f"COMMAND:\n{command}",
    ]
    if wrapper:
        parts.append(
            "\nWRAPPER: oneoff.py detected.\n"
            f"  explanation: {wrapper.get('explanation') or '(empty)'}\n"
            f"  parse_error: {wrapper.get('parse_error')}\n"
            f"  inner_command: {wrapper.get('inner') or '(empty)'}\n"
        )
    else:
        parts.append("\nWRAPPER: (none — command was NOT wrapped by oneoff.py)")
    if walk_errors:
        parts.append("\nWALK_ERRORS:\n" + "\n".join(f"  - {e}" for e in walk_errors))
    if dynamic:
        parts.append("\nDYNAMIC_EVIDENCE:\n" + "\n".join(f"  - {e}" for e in dynamic))
    if files:
        parts.append(f"\nWALKED {len(files)} FILE(S):")
        for f in files:
            parts.append(f"\n--- FILE {f['path']} (sha256:{f['sha256']}) ---\n{f['content']}")
    else:
        parts.append("\nWALKED 0 FILES (no project scripts referenced).")
    payload_text = "\n".join(parts)

    classification, reason, err = _classify_via_llm(api_key, payload_text)
    audit_entry = {
        "ts": ts, "tool": tool_name, "command": command,
        "verdict": classification, "reason": reason, "llm_error": err,
        "cache": "miss", "code_fingerprints": code_hashes,
        "walk_errors": walk_errors, "dynamic_evidence": dynamic,
        "wrapped": bool(wrapper),
    }
    # Never cache verdicts produced from an LLM error path — a transient outage
    # would otherwise pin the operator to gate_approve for every future call
    # against the same payload hash. Only cache verdicts the LLM actually returned.
    if err is None:
        _cache_put(cache_key, classification, reason)
    return _dispatch_outcome(classification, reason, tool_name, command,
                             code_hashes, wrapper, audit_entry)


def _dispatch_outcome(classification: str, reason: str, tool_name: str,
                      command: str, code_hashes: list[str],
                      wrapper: dict | None, audit_entry: dict) -> int:
    if classification == "allow":
        _audit(audit_entry)
        return 0
    if classification == "gate_approve":
        approved = (
            _prompt_approve_inline(command, reason, wrapper)
            if _is_real_interactive_shell()
            else _prompt_approve_browser(tool_name, command, reason, code_hashes, wrapper)
        )
        audit_entry["approve_verdict"] = "approve" if approved else "reject"
        _audit(audit_entry)
        if approved:
            return 0
        deny_reason = f"gate_approve rejected by operator: {reason}"
        _notify_browser(tool_name, command, code_hashes, deny_reason,
                        "gate_approve REJECTED")
        _emit_deny(deny_reason)
        return 0
    # reject — deny immediately, fire-and-forget notify, no operator prompt
    _audit(audit_entry)
    extra = ("<b>Claude did not use the oneoff.py wrapper</b> — this attempt "
             "was auto-denied without an approval prompt. If the intent was "
             "legitimate, Claude must invoke via oneoff.py with a &lt;=70-word "
             "explanation.") if not wrapper else ""
    _notify_browser(tool_name, command, code_hashes, reason, "reject", extra=extra)
    _emit_deny(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
