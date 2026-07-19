# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Rule-006 enforcement worker — LLM-based classification of shell tool calls.

Wired to Claude Code's PreToolUse hook (Bash or PowerShell tool). On every
invocation:

  1. Extract the tool_input.command.
  2. Regex-scan the command text for referenced script file paths
     (Python .py, PowerShell .ps1, shell .sh). Read those files; fail-closed
     if referenced but missing/unreadable/too large.
  3. Compute a cache key from (command text + resolved file contents).
     If the cache holds a verdict, use it — no LLM call.
  4. Otherwise submit the payload to Gemini 2.0 Flash for classification with
     exponential backoff on 429/5xx.
  5. Act:
        allow       → exit 0 silently, tool call proceeds; cache the verdict.
        gate_approve → prompt operator APPROVE (inline TTY or browser); on
                      approve pass through, on reject/timeout deny + browser
                      notification. Cache the verdict either way.
        block_hard  → deny + browser notification, no override. Cache the
                      verdict.

  6. Fail-closed on any of: LLM error/timeout after retries, referenced code
     file missing/unreadable/too large, or the LLM itself returns block_hard
     (which per its prompt covers dynamic-code patterns and compiled binaries
     without inspectable source).

Wiring: direct entry in .claude/settings.json under PreToolUse (Rule-064
pattern). Not routed through chathealthy_enforcement_manager because
PreToolUse needs a specific deny-JSON stdout format the manager does not
preserve.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
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
_LLM_RETRIES = 3
_LLM_BACKOFF_INITIAL_S = 1.5
_APPROVE_TIMEOUT_S = 600
_MAX_FILE_BYTES = 400_000
_MAX_TOTAL_BYTES = 1_500_000

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
# Referenced-file resolution — regex-only, no shell parsing
# ─────────────────────────────────────────────────────────────────────────────
_SCRIPT_PATH_RE = re.compile(
    r"\b(?:python[3]?|bash|sh|pwsh|powershell)\b"   # interpreter token
    r"(?:\s+-\S+(?:\s+\S+)?)*"                       # optional -flag or -flag value pairs
    r"\s+"                                            # then whitespace
    r"([A-Za-z]:[/\\][A-Za-z0-9_./\\-]+\.(?:py|sh|ps1|bash)|"  # abs windows path
    r"[A-Za-z0-9_./\\-]+\.(?:py|sh|ps1|bash))",       # relative/bare path
    re.IGNORECASE,
)


def _resolve_referenced_files(command: str) -> tuple[list[dict], list[str]]:
    """Return (resolved_files, resolution_errors).

    Scans the command text with a plain regex for script-file references and
    tries to read each. Fail-closed reasons: file present in the command but
    missing on disk, unreadable, or larger than the payload cap.

    NOTE: This does NOT try to parse the shell command structure at all —
    the LLM sees the full command text and can judge intent from the surface.
    """
    resolved: list[dict] = []
    seen: set[str] = set()
    errors: list[str] = []
    total_bytes = 0

    for m in _SCRIPT_PATH_RE.finditer(command):
        rel = m.group(1)
        if not rel:
            continue
        if rel.startswith("-"):
            continue
        norm = rel.replace("\\", "/")
        if norm in seen:
            continue
        seen.add(norm)

        abs_path = Path(rel)
        if not abs_path.is_absolute():
            abs_path = (_PROJECT_ROOT / rel).resolve()

        if not abs_path.exists():
            # Only fail-closed when it looks like a real invocation path.
            # `python -m foo.bar` embedded in a heredoc string would not
            # match our regex, so most noise is already filtered.
            errors.append(f"referenced script missing on disk: {rel}")
            continue
        try:
            size = abs_path.stat().st_size
        except OSError as exc:
            errors.append(f"referenced script unreadable: {rel} ({exc})")
            continue
        if size > _MAX_FILE_BYTES:
            errors.append(f"referenced script too large ({size} bytes): {rel}")
            continue
        if total_bytes + size > _MAX_TOTAL_BYTES:
            errors.append(f"total payload cap exceeded at {rel}")
            continue
        try:
            content = abs_path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            errors.append(f"referenced script unreadable: {rel} ({exc})")
            continue
        sha = hashlib.sha256(content.encode("utf-8", errors="replace")).hexdigest()[:16]
        resolved.append({"path": str(abs_path), "content": content, "sha256": sha})
        total_bytes += size

    return resolved, errors


# ─────────────────────────────────────────────────────────────────────────────
# Cache
# ─────────────────────────────────────────────────────────────────────────────
def _payload_hash(command: str, resolved: list[dict]) -> str:
    h = hashlib.sha256()
    h.update(command.encode("utf-8", errors="replace"))
    for f in resolved:
        h.update(b"\x1e")
        h.update(f["path"].encode("utf-8", errors="replace"))
        h.update(b"\x1f")
        h.update(f["sha256"].encode("utf-8"))
    return h.hexdigest()


def _cache_load() -> dict:
    try:
        return json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _cache_get(key: str) -> tuple[str, str] | None:
    d = _cache_load()
    hit = d.get(key)
    if not hit:
        return None
    return hit.get("classification"), hit.get("reason")


def _cache_put(key: str, classification: str, reason: str) -> None:
    try:
        d = _cache_load()
        d[key] = {
            "classification": classification,
            "reason": reason,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(d, indent=2), encoding="utf-8")
    except OSError:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# LLM call — Gemini 2.0 Flash via generativelanguage.googleapis.com
# ─────────────────────────────────────────────────────────────────────────────
_SYSTEM_PROMPT = """You are a security classifier for shell tool invocations.
The ONLY things you protect are: Azure, Cloudflare, MongoDB Atlas, HuggingFace,
Azure Container Registry, and GitHub REMOTE state mutations that bypass the
project's deploy chain (build_chathealthy.py / deploy_chathealthy.py /
promote_chathealthy.py). Everything else is `allow`.

Classify into exactly one of:

  allow       — anything that does NOT mutate remote state on the protected
                targets. This includes:
                  * read-only inspection (list/show/get/describe/query/view/
                    status/info/login/logout/version/help/cat/head/tail/less)
                  * ALL local-filesystem operations (rm, mv, cp, mkdir, rmdir,
                    touch, chmod, ln, echo>file, redirection to local paths)
                  * ALL `git` verbs on the local clone including add, commit,
                    push, checkout, diff, log, status, branch, merge, rebase,
                    stash, reset, tag (git is gated separately at commit time
                    by Rule-065; this classifier MUST NOT second-guess git)
                  * builds, tests, package installs, dependency management
                    (pip install, npm install, docker build, docker tag)
                  * running `build_chathealthy.py` (build produces only local
                    artifacts)
                  * running Python/PowerShell/bash scripts that only touch the
                    local machine, do local file processing, or talk to
                    already-authenticated dev/local services on 127.0.0.1
                    or localhost. Note: a pytest / python / bash wrapper
                    does NOT excuse a script that mutates protected remote
                    state. If you can see (in the script text you were
                    given) any of pymongo insert/update/delete/replace
                    against a real Atlas cluster, `az <write-verb>`,
                    `wrangler <write-verb>`, `atlas <write-verb>`,
                    `docker push`, `gh <write-verb>`, or a POST/PUT/DELETE
                    against Azure Automation webhooks / Atlas Admin API /
                    Cloudflare API / GitHub API, classify as `block_hard`
                    regardless of the invocation being a pytest test
  gate_approve — an invocation of `deploy_chathealthy.py` or
                `promote_chathealthy.py` (deploys/promotes remote state; needs
                operator approval).
  block_hard  — a direct state mutation on Azure/Cloudflare/MongoDB Atlas/HF/
                ACR/GitHub-remote that bypasses the deploy chain, specifically:
                  * `az <group> <write-verb>` where write-verb is create,
                    delete, update, set, deploy, restart, stop, start, scale,
                    assign, revoke, config-zip, purge, etc.
                  * `wrangler deploy`, `wrangler <write-verb>`
                  * `atlas cluster <write-verb>`, `atlas <write-verb>` in general
                  * `docker push` (any registry)
                  * `gh pr <create|merge|edit|close|reopen|comment|review>`,
                    `gh issue <create|close|edit|comment>`, `gh workflow run`,
                    `gh secret <set|delete>`, `gh release <create|edit|delete>`,
                    `gh repo <create|delete|edit>`, `gh api ... -X
                    POST|PUT|DELETE|PATCH`
                  * dynamic-code pattern (exec, eval, compile, downloading and
                    running remote code, writing a file then invoking it)
                  * invocation of a compiled binary whose source is not readable
                    AND that appears to touch remote infra

CATEGORICAL RULE — DO NOT VIOLATE:
Any command whose observable effect is to read or write files on the LOCAL
filesystem (including localhost/127.0.0.1 processes) is `allow`, no matter
what the file is called, what data it contains, or what its intended
downstream use is. That covers `rm`, `mv`, `cp`, `chmod`, shell redirection,
`git` (any verb), Python scripts that edit JSON/YAML/Python/docs, editing
`deployment_architecture.json` or `engineering_rules.json` or any other
manifest or config or catalog file. Editing a manifest is NOT the same as
deploying it. A local file edit does NOT touch Azure/Cloudflare/Atlas/HF/
ACR/GitHub-remote — only invoking `deploy_chathealthy.py` /
`promote_chathealthy.py` or invoking `az`/`wrangler`/`atlas`/`gh` /
`docker push` with a write verb does. If the command's only effect is
local, the answer is `allow`, full stop — do not reason about intent,
downstream use, or manifest semantics; just classify.

Only remote-infra writes bypassing the deploy chain are `block_hard`.

DEPLOY-FROM-GIT RULE (worker enforces before you see the command; do not
override it):
`deploy_chathealthy.py` and `promote_chathealthy.py` invocations with
`--env dev|qa|prod` (anything except `--env local`) MUST come 100% from
git — the working tree must be clean AND HEAD must be at the same commit
as `origin/<branch>`. If either is false, the worker denies BEFORE
consulting you. You should not see such a command; if you do, still
classify as `block_hard`.

If you are UNCERTAIN in any way, return `block_hard`. Fail closed.

Return strict JSON only, no prose:
  {"classification": "allow|gate_approve|block_hard", "reason": "<one sentence>"}
"""


def _classify_via_llm(api_key: str, payload_text: str) -> tuple[str, str, str | None]:
    """Return (classification, reason, error) — error is None on success.

    Retries on transient 429/5xx with exponential backoff up to _LLM_RETRIES.
    """
    # Flat prompt shape — matches find_care_smoke_test.py's proven Gemini call.
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
        try:
            with urllib.request.urlopen(req, timeout=_LLM_TIMEOUT_S) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code} {exc.reason}"
            if exc.code in (429, 500, 502, 503, 504) and attempt < _LLM_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return "block_hard", "LLM HTTP error", last_err
        except (urllib.error.URLError, socket.timeout, TimeoutError) as exc:
            last_err = f"network: {exc}"
            if attempt < _LLM_RETRIES:
                time.sleep(delay)
                delay *= 2
                continue
            return "block_hard", "LLM network error", last_err
        except Exception as exc:  # noqa: BLE001
            return "block_hard", "LLM unexpected error", str(exc)

        try:
            wrapper = json.loads(raw)
            text = wrapper["candidates"][0]["content"]["parts"][0]["text"]
            parsed = json.loads(text)
            cls = parsed.get("classification", "")
            rsn = parsed.get("reason", "")
        except (KeyError, IndexError, ValueError, TypeError) as exc:
            return "block_hard", "LLM response unparsable", f"{exc} :: {raw[:400]}"

        if cls not in ("allow", "gate_approve", "block_hard"):
            return "block_hard", "LLM returned unknown classification", str(cls)
        return cls, rsn or "no reason given", None

    return "block_hard", "LLM retries exhausted", last_err


# ─────────────────────────────────────────────────────────────────────────────
# Browser notification (fire-and-forget)
# ─────────────────────────────────────────────────────────────────────────────
def _notify_browser(tool_name: str, command: str, code_fingerprints: list[str], reason: str, verdict: str) -> None:
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
        f"<p><b>Reason:</b> {reason}</p>"
        f"<p><b>Code fingerprints:</b> "
        + (", ".join(f'<span class="hash">{h}</span>' for h in code_fingerprints) or "none")
        + "</p>"
        "<h2>Blocked command</h2>"
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


def _prompt_approve_inline(command: str, reason: str) -> bool:
    sys.stderr.write(f"\nRule-006 gate_approve: {reason}\nCommand: {command}\nApprove? (type APPROVE) ")
    sys.stderr.flush()
    try:
        reply = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        return False
    return reply.strip() == "APPROVE"


def _prompt_approve_browser(tool_name: str, command: str, reason: str, code_fingerprints: list[str]) -> bool:
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
    except Exception:  # noqa: BLE001
        return False

    verdict: dict[str, str | None] = {"value": None}
    token = secrets.token_urlsafe(8)
    esc_cmd = command.replace("<", "&lt;").replace(">", "&gt;")
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
        f"<p><b>Reason (from classifier):</b> {reason}</p>"
        f"<p><b>Code fingerprints:</b> "
        + (", ".join(f"<code>{h}</code>" for h in code_fingerprints) or "none")
        + "</p>"
        f"<pre>{esc_cmd}</pre>"
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
# Audit log
# ─────────────────────────────────────────────────────────────────────────────
def _audit(entry: dict) -> None:
    try:
        _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:  # noqa: BLE001
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Emit PreToolUse deny JSON
# ─────────────────────────────────────────────────────────────────────────────
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
# main
# ─────────────────────────────────────────────────────────────────────────────
_DEPLOY_ENV_NONLOCAL_RE = re.compile(
    # Must be an actual invocation: python(3) at the head, then a path to
    # deploy_chathealthy.py or promote_chathealthy.py, then --env in a
    # short window (no shell separators, no newlines). Won't match strings
    # embedded inside heredocs or quoted commit messages.
    r"(?:^|[\r\n]|(?<=[|;&])\s*)"
    r"python[3]?\s+"
    r"[^\s\n|;&]*(?:deploy|promote)_chathealthy\.py\b"
    r"[^|;&\n\r]{0,300}--env\s+(dev|qa|prod)\b",
    re.IGNORECASE,
)


def _git_state_ok_for_deploy() -> tuple[bool, str]:
    """Return (ok, reason). ok=True means working tree is clean AND HEAD is at
    origin/<branch>. Anything else fails closed with a human-readable reason."""
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


def _load_allowed_patterns() -> list[str]:
    """Read Rule-006's scope entry ["_llm_classify", "allowed_pattern", [...]]
    from brain/machine_artifacts/content/engineering_rules.json. Regexes;
    any command that regex-matches skips LLM classification and is allowed.
    Silent fall-through to empty list on read error."""
    try:
        p = _PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "engineering_rules.json"
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
        for rule in (data.get("rules") or {}).get("rule", []):
            if rule.get("id") != "Rule-006":
                continue
            for enf in (rule.get("enforcements") or {}).get("enforcement", []):
                for scope in enf.get("scopes") or []:
                    if (
                        isinstance(scope, list)
                        and len(scope) == 3
                        and scope[0] == "_llm_classify"
                        and scope[1] == "allowed_pattern"
                        and isinstance(scope[2], list)
                    ):
                        return [str(s) for s in scope[2]]
    except Exception:  # noqa: BLE001
        pass
    return []


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

    # Deploy-from-git rule: any deploy_chathealthy.py or promote_chathealthy.py
    # invocation targeting dev/qa/prod MUST be from a clean working tree AT
    # origin/<branch>. This runs BEFORE the LLM classifier because the LLM
    # cannot see git state. Local-env deploys (--env local) are exempt.
    if _DEPLOY_ENV_NONLOCAL_RE.search(command):
        ok, reason = _git_state_ok_for_deploy()
        if not ok:
            deny_reason = (
                f"Rule-006 deploy-from-git: {reason}. Non-local deploys MUST "
                f"come 100% from git. Commit + push before invoking deploy."
            )
            _audit({"ts": datetime.now(timezone.utc).isoformat(), "tool": tool_name,
                    "command": command, "verdict": "block_hard", "reason": deny_reason})
            _notify_browser(tool_name, command, [], deny_reason, "block_hard")
            _emit_deny(deny_reason)
            return 0

    # Operator-declared allow list. Rule-006's scope in engineering_rules.json
    # may carry ["_llm_classify", "allowed_pattern", ["<regex>", ...]] — any
    # command whose text is a regex-search match for one of the listed
    # patterns skips LLM classification and is allowed. Patterns must be
    # operator-approved (their addition to the rules JSON is subject to
    # Rule-007's identity approval flow for governance-substrate edits).
    # Used for tightly-scoped operator-owned scripts that need to mutate
    # remote state as part of the normal deploy/ops workflow but do not fit
    # the deploy chain proper.
    for pattern in _load_allowed_patterns():
        if pattern and re.search(pattern, command):
            _audit({"ts": datetime.now(timezone.utc).isoformat(), "tool": tool_name,
                    "command": command, "verdict": "allow",
                    "reason": f"matched allowed pattern {pattern!r}"})
            return 0

    api_key = _load_env_var("GEMINI_API_KEY") or _load_env_var("GOOGLE_API_KEY")
    if not api_key:
        reason = "Rule-006 misconfigured: GEMINI_API_KEY not found in Code/.env"
        _audit({"ts": datetime.now(timezone.utc).isoformat(), "tool": tool_name,
                "command": command, "verdict": "block_hard", "reason": reason})
        _notify_browser(tool_name, command, [], reason, "block_hard")
        _emit_deny(reason)
        return 0

    resolved, errors = _resolve_referenced_files(command)
    if errors:
        reason = "fail-closed on resolution: " + "; ".join(errors[:3])
        _audit({"ts": datetime.now(timezone.utc).isoformat(), "tool": tool_name,
                "command": command, "verdict": "block_hard", "reason": reason,
                "resolution_errors": errors})
        _notify_browser(tool_name, command, [f["sha256"] for f in resolved], reason, "block_hard")
        _emit_deny(reason)
        return 0

    code_hashes = [f["sha256"] for f in resolved]
    cache_key = _payload_hash(command, resolved)
    cached = _cache_get(cache_key)
    if cached:
        classification, reason = cached
        audit_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "tool": tool_name, "command": command,
            "verdict": classification, "reason": reason,
            "cache": "hit", "code_fingerprints": code_hashes,
        }
        if classification == "allow":
            _audit(audit_entry)
            return 0
        if classification == "gate_approve":
            approved = (
                _prompt_approve_inline(command, reason)
                if _is_real_interactive_shell()
                else _prompt_approve_browser(tool_name, command, reason, code_hashes)
            )
            audit_entry["approve_verdict"] = "approve" if approved else "reject"
            _audit(audit_entry)
            if approved:
                return 0
            deny_reason = f"gate_approve denied: {reason}"
            _notify_browser(tool_name, command, code_hashes, deny_reason, "gate_approve REJECTED")
            _emit_deny(deny_reason)
            return 0
        _audit(audit_entry)
        _notify_browser(tool_name, command, code_hashes, reason, "block_hard")
        _emit_deny(reason)
        return 0

    parts: list[str] = [f"TOOL: {tool_name}", f"COMMAND:\n{command}"]
    for f in resolved:
        parts.append(f"\n--- FILE {f['path']} (sha256:{f['sha256']}) ---\n{f['content']}")
    payload_text = "\n".join(parts)

    classification, reason, err = _classify_via_llm(api_key, payload_text)
    audit_entry = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "tool": tool_name, "command": command,
        "verdict": classification, "reason": reason, "llm_error": err,
        "cache": "miss", "code_fingerprints": code_hashes,
    }
    _cache_put(cache_key, classification, reason)

    if classification == "allow":
        _audit(audit_entry)
        return 0

    if classification == "gate_approve":
        approved = (
            _prompt_approve_inline(command, reason)
            if _is_real_interactive_shell()
            else _prompt_approve_browser(tool_name, command, reason, code_hashes)
        )
        audit_entry["approve_verdict"] = "approve" if approved else "reject"
        _audit(audit_entry)
        if approved:
            return 0
        deny_reason = f"gate_approve denied: {reason}"
        _notify_browser(tool_name, command, code_hashes, deny_reason, "gate_approve REJECTED")
        _emit_deny(deny_reason)
        return 0

    _audit(audit_entry)
    _notify_browser(tool_name, command, code_hashes, reason, "block_hard")
    _emit_deny(reason)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
