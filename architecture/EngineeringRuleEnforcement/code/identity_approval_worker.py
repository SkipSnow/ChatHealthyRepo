# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Rule-007 enforcement worker — identity/role manifest git-delta approval.

Fires on pre-commit. Inspects the staged version of
brain/machine_artifacts/content/deployment_architecture.json against HEAD.
If any of the following identity/RBAC surfaces changed, the worker requires
an explicit operator APPROVE echo before letting the commit through:

    - IdentityCatalog (adds / removes / any per-item field change)
    - CustomRoleCatalog (adds / removes / any per-item field change)
    - Per-target allowed_roles[] (any change on any DeploymentTargetRecord)

If none of those surfaces changed, the worker exits 0 silently.

Approval mechanism follows the Rule-065 CommitAuthorizationWorker pattern:
inline stderr prompt in a real interactive shell; otherwise a browser page
opened on 127.0.0.1 that the operator clicks to approve/reject.
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
from typing import Any
from urllib.parse import urlparse, parse_qs

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

# Below the block that resolves the library, not above it.
from chathealthy_lib.exceptions import ChatHealthyException


_AGENT_MARKERS = ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_CODE_ENTRYPOINT")
_BROWSER_TIMEOUT_SECONDS = 600

_MANIFEST_REL = "brain/machine_artifacts/content/deployment_architecture.json"

_AUDIT_LOG_PATH = (
    _THIS_FILE.parent.parent
    / "ArchitectureDesignAndAuditDocs"
    / "identity_approval.log"
)


class IdentityApprovalWorker(EnforcementWorker):
    """Rule-007: manifest identity/role delta requires operator echo."""

    SCOPE_DEFAULT = True

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.violation_count: int = 0

    # ────────────────────────────────────────────────────────────────────────
    def run(self) -> int:
        summary = self._compute_identity_delta_summary()
        if summary is None:
            return EXIT_OK

        if self._is_real_interactive_shell():
            return self._prompt_inline(summary)
        return self._prompt_via_browser(summary)

    # ────────────────────────────────────────────────────────────────────────
    def _compute_identity_delta_summary(self) -> str | None:
        """Return a human-readable summary of identity/RBAC changes, or None
        if nothing identity-related changed."""
        head_manifest = self._load_head_manifest()
        staged_manifest = self._load_staged_manifest()
        if staged_manifest is None:
            return None

        head_ic = _by_id(head_manifest, "IdentityCatalog", "identity_id")
        stg_ic = _by_id(staged_manifest, "IdentityCatalog", "identity_id")
        ic_added, ic_removed, ic_modified = _diff_dicts(head_ic, stg_ic)

        head_crc = _by_id(head_manifest, "CustomRoleCatalog", "role_id")
        stg_crc = _by_id(staged_manifest, "CustomRoleCatalog", "role_id")
        crc_added, crc_removed, crc_modified = _diff_dicts(head_crc, stg_crc)

        head_ar = _allowed_roles_by_target(head_manifest)
        stg_ar = _allowed_roles_by_target(staged_manifest)
        ar_changed = sorted(
            tid for tid in set(head_ar) | set(stg_ar)
            if head_ar.get(tid, []) != stg_ar.get(tid, [])
        )

        if not (ic_added or ic_removed or ic_modified
                or crc_added or crc_removed or crc_modified
                or ar_changed):
            return None

        lines: list[str] = ["Identity/RBAC surface changes in deployment_architecture.json:"]
        for label, added, removed, modified in (
            ("IdentityCatalog", ic_added, ic_removed, ic_modified),
            ("CustomRoleCatalog", crc_added, crc_removed, crc_modified),
        ):
            if added:
                lines.append(f"  {label} + added ({len(added)}): " + ", ".join(sorted(added)))
            if removed:
                lines.append(f"  {label} - removed ({len(removed)}): " + ", ".join(sorted(removed)))
            if modified:
                lines.append(f"  {label} ~ modified ({len(modified)}): " + ", ".join(sorted(modified)))
        if ar_changed:
            lines.append(
                f"  Per-target allowed_roles changed ({len(ar_changed)}): "
                + ", ".join(ar_changed)
            )
        return "\n".join(lines)

    # ────────────────────────────────────────────────────────────────────────
    def _load_head_manifest(self) -> dict[str, Any]:
        try:
            out = subprocess.check_output(
                ["git", "show", f"HEAD:{_MANIFEST_REL}"],
                stderr=subprocess.DEVNULL,
            )
            return json.loads(out.decode("utf-8"))
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            return {}

    def _load_staged_manifest(self) -> dict[str, Any] | None:
        """Return the staged version of the manifest, or None if the manifest
        is not part of this commit."""
        try:
            names = subprocess.check_output(
                ["git", "diff", "--cached", "--name-only"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8").splitlines()
        except subprocess.CalledProcessError:
            return None
        if _MANIFEST_REL not in [n.strip() for n in names]:
            return None
        try:
            out = subprocess.check_output(
                ["git", "show", f":{_MANIFEST_REL}"],
                stderr=subprocess.DEVNULL,
            )
            return json.loads(out.decode("utf-8"))
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            raise ChatHealthyException(
            "worker_internal",
                f"could not load staged {_MANIFEST_REL}: {exc}"
            )

    # ────────────────────────────────────────────────────────────────────────
    def _is_real_interactive_shell(self) -> bool:
        for marker in _AGENT_MARKERS:
            if os.environ.get(marker):
                return False
        return sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty()

    def _prompt_inline(self, summary: str) -> int:
        sys.stderr.write(summary + "\nApprove? (type APPROVE) ")
        sys.stderr.flush()
        try:
            reply = sys.stdin.readline()
        except (KeyboardInterrupt, EOFError):
            self._reject(summary, "interrupted")
            return EXIT_VIOLATIONS_FOUND
        if reply.strip() == "APPROVE":
            self._audit(summary, "approve", "inline")
            return EXIT_OK
        self._reject(summary, "not approved")
        return EXIT_VIOLATIONS_FOUND

    def _prompt_via_browser(self, summary: str) -> int:
        try:
            with socket.socket() as sock:
                sock.bind(("127.0.0.1", 0))
                port = sock.getsockname()[1]
        except Exception as exc:
            self._reject(summary, f"could not allocate approval port: {exc}")
            return EXIT_VIOLATIONS_FOUND

        verdict = {"value": None}
        token = secrets.token_urlsafe(8)
        summary_html = summary.replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br>")

        page_html = (
            "<!doctype html><html><head><meta charset=utf-8>"
            "<title>ChatHealthy identity/RBAC approval</title>"
            "<style>body{font-family:system-ui,sans-serif;padding:40px;"
            "background:#5b21b6;color:#fff;text-align:left;max-width:900px;margin:auto}"
            "h1{font-size:24px}pre{background:#00000033;padding:16px;border-radius:6px;"
            "white-space:pre-wrap}"
            "button{font-size:18px;padding:14px 32px;margin:8px;border:none;"
            "border-radius:6px;cursor:pointer;font-weight:600}"
            ".approve{background:#0b9a94;color:#fff}"
            ".reject{background:#dc2626;color:#fff}</style></head>"
            "<body>"
            "<h1>Approve identity/RBAC manifest change?</h1>"
            f"<pre>{summary_html}</pre>"
            f"<p>Token: <code>{token}</code></p>"
            f"<form id=decide_form method=POST action=/decide>"
            f"<button class=approve name=verdict value=approve type=submit>APPROVE</button>"
            f"<button class=reject name=verdict value=reject type=submit>REJECT</button>"
            f"<input type=hidden name=token value=\"{token}\"></form>"
            "<script>"
            "document.getElementById('decide_form').addEventListener('submit',async function(e){"
            "e.preventDefault();"
            "var fd=new FormData(e.target);"
            "if(e.submitter&&e.submitter.name)fd.set(e.submitter.name,e.submitter.value);"
            "try{"
            "var r=await fetch('/decide',{method:'POST',body:new URLSearchParams(fd)});"
            "if(r.ok){window.close();document.body.innerHTML='<h1>Recorded.</h1>';}"
            "else{document.body.innerHTML='<h1>Error '+r.status+'</h1>';}"
            "}catch(err){document.body.innerHTML='<h1>Network error</h1>';}"
            "});"
            "</script>"
            "</body></html>"
        )
        ack_html = "<!doctype html><h1 style=\"font-family:system-ui;padding:40px;text-align:center\">Recorded.</h1>"

        class _Handler(BaseHTTPRequestHandler):
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
            self._reject(summary, f"could not start approval server: {exc}")
            return EXIT_VIOLATIONS_FOUND

        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{port}/prompt"
        try:
            webbrowser.open_new(url)
        except Exception:
            pass
        sys.stderr.write(f"\nIdentity/RBAC approval requested at: {url}\n")
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
            self._audit(summary, "approve", "browser")
            return EXIT_OK
        if verdict["value"] == "reject":
            self._reject(summary, "rejected by human")
            return EXIT_VIOLATIONS_FOUND
        self._reject(summary, f"approval timed out after {_BROWSER_TIMEOUT_SECONDS}s")
        return EXIT_VIOLATIONS_FOUND

    # ────────────────────────────────────────────────────────────────────────
    def _reject(self, summary: str, reason: str) -> None:
        self._audit(summary, "reject", reason)
        self._emit_violation(ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id=self.rule_id,
            resource=_MANIFEST_REL,
            message=f"identity/RBAC delta not approved: {reason}",
            severity="error",
        ))
        self.violation_count = 1

    def _audit(self, summary: str, verdict: str, reason: str) -> None:
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "hook": self.hook,
            "enforcement_id": self.enforcement_id,
            "verdict": verdict,
            "reason": reason,
            "summary": summary,
            "pid": os.getpid(),
        }
        try:
            _AUDIT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_LOG_PATH.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception:
            pass


def _by_id(manifest: dict[str, Any], top_key: str, id_field: str) -> dict[str, dict[str, Any]]:
    items = manifest.get(top_key, []) or []
    return {item[id_field]: item for item in items if isinstance(item, dict) and id_field in item}


def _allowed_roles_by_target(manifest: dict[str, Any]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for rec in manifest.get("DeploymentTargetRecord", []) or []:
        if not isinstance(rec, dict):
            continue
        tid = rec.get("target_id")
        if not tid:
            continue
        out[tid] = sorted(rec.get("allowed_roles", []) or [])
    return out


def _diff_dicts(head: dict[str, Any], staged: dict[str, Any]) -> tuple[set[str], set[str], set[str]]:
    added = set(staged) - set(head)
    removed = set(head) - set(staged)
    modified = {k for k in set(head) & set(staged) if head[k] != staged[k]}
    return added, removed, modified


def main(argv: list[str] | None = None) -> int:
    return IdentityApprovalWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
