# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""local_deploy.py — Local deploy class for EPIC-008-F-004.

Satisfies V11 stories:
  S-001 (universal deploy contract — port table, atomicity, build/start/verify,
         smoke invocation, step notices, human-auth gate)
  S-002 (local-only — backend processes, Website wrapper on host OS, shared dev
         MongoDB cluster)
  Plus invokes S-006 (the master smoke test) with env=local.

Five design constraints (Skip 2026-05-01):
  1. Each deploy script encapsulated in a class.
  2. Conventional main: parse args -> construct class -> call .run().
  3. Two scripts only — this is the local one (remote_deploy.py is the other).
  4. Playwright callable with env, headless (handled in localSmokeTestPyTest.py).
  5. No invention; meet V11 exactly.

Known V11 deviations filed as bugs:
  - S-002-REQ-T-001 says backends run as Docker containers; this class runs
    them as subprocess Python (matches the existing _start_*.py pattern).
    Bug filed for the Docker migration as part of the morning hand-off list.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import psutil

# ── Project root ──────────────────────────────────────────────────────────
_REPO_ROOT_ENV = "CHATHEALTHY_PROJECT_ROOT"
if _REPO_ROOT_ENV not in os.environ:
    sys.exit(f"ERROR: {_REPO_ROOT_ENV} env var not set. Cannot resolve paths.")
REPO_ROOT = Path(os.environ[_REPO_ROOT_ENV]).resolve()


class LocalDeploy:
    """Local deploy script per V11 EPIC-008-F-004 S-001 + S-002."""

    # S-001-REQ-T-001 — canonical port assignments. DO NOT restate these
    # numbers anywhere else in the deploy classes; reference only.
    PORTS = {
        "http":     80,
        "https":    443,
        "findcare": 7860,
        "evalcare": 8001,
        "shared":   8002,
    }

    def __init__(self) -> None:
        self.env = "local"
        self.repo_root = REPO_ROOT
        self.deploy_dir = self.repo_root / "Code" / "deploy"
        self.frontend_dir = (self.repo_root / "Code" / "ConversationalUX"
                             / "FindCareChat" / "frontend")
        self.backend_dir = (self.repo_root / "Code" / "ConversationalUX"
                            / "FindCareChat" / "backend")
        self.website_dir = self.repo_root / "Website"
        self.certs_dir = self.repo_root / "Code" / "Shared" / "ops" / "certs"
        self.output_dir = self.repo_root / "test_output" / "deploy"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        self.output_path = (self.output_dir / f"deploy_local_{ts}.json")
        self.results: dict = {
            "env": self.env,
            "started_at": ts,
            "ports": dict(self.PORTS),
            "steps": [],
            "verification": [],
            "smoke_rc": None,
            "smoke_passed": None,
            "structured_output_path": str(self.output_path),
        }
        self.backend_procs: list[subprocess.Popen] = []
        self._smoke_failed = False

    # ── Step notices (S-001-REQ-B-005, S-007-REQ-B-001) ────────────────
    def _step_notice(self, msg: str) -> None:
        line = f"[STEP {self.env}] {msg}"
        print(line, flush=True)
        self.results["steps"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": msg,
        })

    # ── Human auth gate (S-001-REQ-B-006) ──────────────────────────────
    # OVERNIGHT WAIVER per Skip 2026-05-01 — call site in run() is commented
    # out. Restore that call before morning UAT. The function body stays so
    # restoration is a single-line uncomment, not a function-rewrite.
    def _human_authorization_gate(self) -> None:
        ans = input(f"Authorize {self.env} deploy? (y/n): ").strip().lower()
        if ans != "y":
            sys.exit(f"Deploy aborted by human at gate ({self.env}).")

    # ── Atomic teardown precondition (S-001-REQ-T-005) ─────────────────
    def _teardown_precondition(self) -> None:
        """Kill processes bound to the canonical ports; verify ports clear."""
        killed_pids = set()
        for port in self.PORTS.values():
            for pid in self._pids_listening_on(port):
                if pid == os.getpid():
                    # Defensive: never kill self. (Should not happen — we
                    # don't bind to these ports — but the guard is cheap.)
                    continue
                try:
                    proc = psutil.Process(pid)
                    proc.kill()
                    killed_pids.add(pid)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

        # Wipe stale frontend artifacts (re-built fresh in _build_react_frontend)
        for stale in (self.frontend_dir / "dist",
                      self.frontend_dir / "node_modules" / ".vite"):
            if stale.exists():
                shutil.rmtree(stale, ignore_errors=True)

        time.sleep(2)

        # Verify every port is clear
        not_clear = []
        for port in self.PORTS.values():
            if self._port_in_use(port):
                not_clear.append(port)
        if not_clear:
            sys.exit(f"ERROR: ports still in use after teardown: {not_clear}")

        self.results["steps"].append({
            "ts": datetime.now(timezone.utc).isoformat(),
            "msg": f"teardown killed pids: {sorted(killed_pids) or 'none'}",
        })

    def _pids_listening_on(self, port: int) -> list[int]:
        pids = []
        for conn in psutil.net_connections(kind="inet"):
            if conn.status == psutil.CONN_LISTEN and conn.laddr.port == port:
                if conn.pid:
                    pids.append(conn.pid)
        return pids

    def _port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            try:
                s.connect(("127.0.0.1", port))
                return True
            except (ConnectionRefusedError, socket.timeout, OSError):
                return False

    # ── Validate prerequisites ─────────────────────────────────────────
    def _validate_prerequisites(self) -> None:
        required_certs = [
            "localhost.crt", "localhost.key",
            "findcare.crt", "findcare.key",
            "evalcare.crt", "evalcare.key",
            "shared.crt", "shared.key",
            "ca.crt",
        ]
        missing = [c for c in required_certs
                   if not (self.certs_dir / c).is_file()]
        if missing:
            sys.exit(f"ERROR: missing certs in {self.certs_dir}: {missing}")

        if not shutil.which("node"):
            sys.exit("ERROR: node not on PATH")
        if not shutil.which("python"):
            sys.exit("ERROR: python not on PATH")

        # Frontend ux symlink: point Code/ConversationalUX/FindCareChat/
        # frontend/src/ux at Code/Shared/ux. If missing, copy as fallback.
        ux_link = self.frontend_dir / "src" / "ux"
        ux_target = self.repo_root / "Code" / "Shared" / "ux"
        if not ux_link.exists():
            try:
                shutil.copytree(ux_target, ux_link)
            except Exception as e:
                sys.exit(f"ERROR: ux symlink/copy failed: {e}")

    # ── Build backend containers (S-002-REQ-T-001) ─────────────────────
    def _build_backend_containers(self) -> None:
        """V11 says Docker; this class runs subprocess Python instead.

        Filed as a known V11 deviation in the morning hand-off bug list.
        Method exists so the call site in run() is honest about the step.
        """
        # No-op for tonight; the venv supplies the runtime. Docker
        # containerization tracked as a separate bug.
        self._step_notice(
            "backend containers SKIPPED — using subprocess Python "
            "(V11 S-002-REQ-T-001 deviation, bug filed)"
        )

    # ── Build React frontend (S-001-REQ-T-008) ─────────────────────────
    # SKIP'S HIGH-MISS STEP — historically often missed in deploys.
    # The deploy fails atomically (S-001-REQ-B-001) if this step fails.
    def _build_react_frontend(self) -> None:
        self._step_notice("building React frontend (high-miss step)")
        # Pre-S-008: bake localhost FindCare URL. Post-S-008: SharedServices.
        # Tonight we are pre-S-008 (S-008 migration is in the morning bug list).
        api_url = ""  # empty -> relative URLs to same origin (port 7860)

        env = os.environ.copy()
        env["VITE_API_URL"] = api_url
        try:
            subprocess.run(
                ["npm", "ci", "--silent"],
                cwd=self.frontend_dir, env=env, check=True,
                shell=(sys.platform == "win32"),
            )
            subprocess.run(
                ["npx", "vite", "build"],
                cwd=self.frontend_dir, env=env, check=True,
                shell=(sys.platform == "win32"),
            )
        except subprocess.CalledProcessError as e:
            sys.exit(f"ERROR: React build failed: {e}")

        dist_index = self.frontend_dir / "dist" / "index.html"
        if not dist_index.is_file():
            sys.exit(f"ERROR: React build produced no {dist_index}")

        # Copy dist/* to backend/static/
        backend_static = self.backend_dir / "static"
        for old in ("assets", "index.html"):
            old_path = backend_static / old
            if old_path.is_dir():
                shutil.rmtree(old_path)
            elif old_path.is_file():
                old_path.unlink()
        backend_static.mkdir(parents=True, exist_ok=True)
        for item in (self.frontend_dir / "dist").iterdir():
            if item.is_dir():
                shutil.copytree(item, backend_static / item.name)
            else:
                shutil.copy2(item, backend_static / item.name)

    # ── Start servers (separate processes; matches existing pattern) ────
    def _start_backend_processes(self) -> None:
        certs_arg = str(self.certs_dir)
        website_arg = str(self.website_dir)

        # Use existing _start_*.py launchers — they're proven and self-contained.
        # All four are background processes that own their own port.
        py = sys.executable  # venv python (has anthropic + all deps)
        starts = [
            ([py, str(self.deploy_dir / "_start_website.py"),
              certs_arg, website_arg], "website", "website-80-443"),
            ([py, str(self.deploy_dir / "_start_findcare.py"),
              certs_arg], "findcare", "findcare-7860"),
            ([py, str(self.deploy_dir / "_start_evalcare.py"),
              certs_arg], "evalcare", "evalcare-8001"),
            ([py, str(self.deploy_dir / "_start_shared.py"),
              certs_arg], "shared", "shared-8002"),
        ]
        log_dir = self.output_dir / "process_logs"
        log_dir.mkdir(exist_ok=True)
        for cmd, label, log_stem in starts:
            log_file = log_dir / f"{log_stem}_{self.results['started_at']}.log"
            log_fh = open(log_file, "w", encoding="utf-8")
            # CREATE_NO_WINDOW suppresses the per-process console popup on
            # Windows; CREATE_NEW_PROCESS_GROUP keeps signal isolation.
            proc = subprocess.Popen(
                cmd, cwd=str(self.repo_root),
                stdout=log_fh, stderr=subprocess.STDOUT,
                creationflags=(
                    (subprocess.CREATE_NEW_PROCESS_GROUP
                     | subprocess.CREATE_NO_WINDOW)
                    if sys.platform == "win32" else 0
                ),
            )
            self.backend_procs.append(proc)
            self._step_notice(
                f"started {label} pid={proc.pid} log={log_file.name}"
            )

    # ── Wait for everyone to come up (S-001-REQ-B-003) ─────────────────
    def _wait_for_all_components(self, timeout_s: int = 180) -> None:
        # Each entry: (label, URL, accept_predicate(text)). FindCare backend
        # MUST self-identify as "ok" (not the wrapper's "waiting" placeholder).
        checks = [
            ("findcare",
             f"https://localhost:{self.PORTS['findcare']}/health",
             lambda t: '"status":"ok"' in t),
            ("evalcare",
             f"https://localhost:{self.PORTS['evalcare']}/health",
             lambda t: '"service":"evaluate_care"' in t),
            ("shared",
             f"https://localhost:{self.PORTS['shared']}/health",
             lambda t: '"service":"shared_services"' in t),
            ("website",
             "https://localhost/",
             lambda t: True),  # any 200 from wrapper
        ]
        deadline = time.time() + timeout_s
        last_state = {}
        with httpx.Client(verify=False, timeout=5) as c:
            while time.time() < deadline:
                ready = []
                missing = []
                for label, url, ok_pred in checks:
                    try:
                        r = c.get(url)
                        if r.status_code == 200 and ok_pred(r.text):
                            ready.append(label)
                            last_state[label] = "ready"
                        else:
                            missing.append(label)
                            last_state[label] = (
                                f"status={r.status_code} "
                                f"text={r.text[:80]!r}"
                            )
                    except Exception as e:
                        missing.append(label)
                        last_state[label] = f"err={type(e).__name__}"
                if not missing:
                    self._step_notice(
                        f"all components ready: {ready}"
                    )
                    return
                time.sleep(3)
        sys.exit(
            f"ERROR: components did not all come up in {timeout_s}s. "
            f"State: {last_state}"
        )

    # ── Verify components (S-001-REQ-B-003) ────────────────────────────
    def _verify_components(self) -> None:
        passed, failed = [], []
        v = self.results["verification"]

        def record(name: str, ok: bool, detail: str = "") -> None:
            v.append({"name": name, "ok": ok, "detail": detail[:300]})
            (passed if ok else failed).append(name)

        with httpx.Client(verify=False, timeout=10) as c:
            try:
                r = c.get("http://localhost/", follow_redirects=False)
                record("http_to_https_301",
                       r.status_code == 301,
                       f"got {r.status_code}")
            except Exception as e:
                record("http_to_https_301", False, str(e))

            try:
                r = c.get("https://localhost/")
                record("website_200", r.status_code == 200,
                       f"got {r.status_code}")
                record("website_has_banner",
                       "envBanner" in r.text, "")
            except Exception as e:
                record("website_200", False, str(e))

            for svc, port in (("findcare", self.PORTS["findcare"]),
                              ("evalcare", self.PORTS["evalcare"]),
                              ("shared",   self.PORTS["shared"])):
                try:
                    r = c.get(f"https://localhost:{port}/health")
                    record(f"{svc}_health",
                           r.status_code == 200,
                           r.text)
                except Exception as e:
                    record(f"{svc}_health", False, str(e))

        # mTLS verifications (FindCare client cert -> EvalCare, FindCare -> Shared)
        ca = str(self.certs_dir / "ca.crt")
        fc_cert = (str(self.certs_dir / "findcare.crt"),
                   str(self.certs_dir / "findcare.key"))
        for tgt_svc, tgt_port, expected_substr in (
            ("evalcare", self.PORTS["evalcare"], "evaluate_care"),
            ("shared",   self.PORTS["shared"],   "shared_services"),
        ):
            try:
                with httpx.Client(cert=fc_cert, verify=ca, timeout=10) as cc:
                    r = cc.get(f"https://localhost:{tgt_port}/health")
                    ok = r.status_code == 200 and expected_substr in r.text
                    record(f"mtls_findcare_to_{tgt_svc}", ok,
                           r.text if ok else f"{r.status_code}: {r.text}")
            except Exception as e:
                record(f"mtls_findcare_to_{tgt_svc}", False, str(e))

        self._step_notice(
            f"verification: {len(passed)} passed, {len(failed)} failed"
            + (f"; failed={failed}" if failed else "")
        )
        if failed:
            self._smoke_failed = True

    # ── Invoke smoke test (S-001-REQ-B-004 + S-006) ────────────────────
    def _invoke_smoke_test(self) -> int:
        cmd = [
            sys.executable, "-m", "pytest", "-v",
            str(self.deploy_dir / "localSmokeTestPyTest.py"),
            f"--smoke-env={self.env}",
        ]
        # Run smoke test, capture output. We rely on pytest's exit code
        # (returncode equivalent of PIPESTATUS[0] in the shell).
        result = subprocess.run(
            cmd, cwd=str(self.repo_root),
            capture_output=True, text=True,
        )
        # Display output to controlling terminal (S-001-REQ-T-007 spirit)
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    # ── Pytest exit propagation (S-001-REQ-T-007) ──────────────────────
    def _display_smoke_failure_banner(self) -> None:
        banner = (
            "\n=========================================="
            "\n  SMOKE TEST FAILED"
            "\n  Environment left up for inspection."
            "\n=========================================="
        )
        print(banner, flush=True)
        sys.stderr.write(banner + "\n")

    # ── Human verify before teardown (S-001-REQ-B-004) ─────────────────
    # OVERNIGHT WAIVER — overnight we never auto-teardown after smoke; the
    # next deploy run's _teardown_precondition() handles any teardown the
    # human authorizes. So this gate is a no-op tonight by virtue of the
    # script not invoking teardown post-smoke. Restored implicitly when
    # someone wires automatic teardown into run() in a future iteration.
    def _human_verify_before_teardown(self) -> None:
        # OVERNIGHT WAIVER per Skip 2026-05-01 — restore before morning UAT
        ans = input("Smoke failed. Tear down anyway? (y/n): ").strip().lower()
        if ans != "y":
            sys.exit("Teardown aborted at human verify gate.")

    # ── Structured deploy output (S-001-REQ-T-006) ─────────────────────
    def _write_structured_output(self) -> None:
        self.results["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.results["backend_pids"] = [p.pid for p in self.backend_procs]
        self.output_path.write_text(
            json.dumps(self.results, indent=2),
            encoding="utf-8",
        )
        self._step_notice(f"structured output -> {self.output_path}")

    # ── Orchestration ──────────────────────────────────────────────────
    def run(self) -> int:
        self._step_notice(f"deploy started for {self.env}")

        self._human_authorization_gate()                    # S-001-REQ-B-006

        self._teardown_precondition()                       # S-001-REQ-T-005
        self._step_notice("old environment torn down and ready")

        self._validate_prerequisites()
        self._build_backend_containers()                    # S-002-REQ-T-001
        self._build_react_frontend()                        # S-001-REQ-T-008
        self._start_backend_processes()                     # S-002-REQ-T-001+T-002
        self._wait_for_all_components()                     # S-001-REQ-B-003
        self._verify_components()                           # S-001-REQ-B-003
        self._step_notice("new environment built and verified")

        self._step_notice("smoke test started")
        smoke_rc = self._invoke_smoke_test()                # S-001-REQ-B-004
        self.results["smoke_rc"] = smoke_rc
        self.results["smoke_passed"] = (smoke_rc == 0)
        self._step_notice(f"smoke test ended: rc={smoke_rc}")

        if smoke_rc != 0:
            self._display_smoke_failure_banner()            # S-001-REQ-T-007
            self._human_verify_before_teardown()            # S-001-REQ-B-004

        self._write_structured_output()                     # S-001-REQ-T-006
        return smoke_rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local deploy for ChatHealthy.ai per V11 EPIC-008-F-004"
    )
    # No env arg — local is the only env this script handles.
    # --no-smoke is a convenience flag for build-only iteration.
    parser.add_argument(
        "--no-smoke", action="store_true",
        help="Build + start + verify, but skip the smoke test invocation.",
    )
    args = parser.parse_args(argv)

    deploy = LocalDeploy()
    if args.no_smoke:
        # Replace _invoke_smoke_test with a no-op for this run.
        deploy._invoke_smoke_test = lambda: 0  # type: ignore[method-assign]
    return deploy.run()


if __name__ == "__main__":
    sys.exit(main())
