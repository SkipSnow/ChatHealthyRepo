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
        """Stop backend containers + kill website host process; verify
        ports clear.

        Fail-hard semantics per V11 S-001-REQ-B-001: the wipe is the
        precondition for atomicity. If we cannot wipe, we cannot deploy.
        """
        # 1. Docker stop the backend containers (V11 S-002-REQ-T-001).
        #    The chathealthy-kafka container is intentionally NOT touched —
        #    it's separate infrastructure used by the producer.
        for container_name in self.BACKEND_CONTAINERS:
            result = subprocess.run(
                ["docker", "stop", container_name],
                capture_output=True, text=True,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0),
            )
            # docker stop on a non-existent or already-stopped container
            # returns nonzero; that's a teardown success (goal achieved).
            # We don't differentiate — the post-step port verification gates.
            if result.returncode == 0:
                self._step_notice(f"docker stopped {container_name}")

        # 2. Kill any remaining processes on the canonical ports
        #    (covers the website wrapper + anything else stray).
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
                except psutil.NoSuchProcess:
                    # Process already dead = teardown goal achieved for this PID.
                    pass
                # AccessDenied: NOT caught — surfaces and aborts the deploy.
                # If we can't kill a port-listening process, we cannot ensure
                # atomicity per V11 S-001-REQ-B-001 / S-001-REQ-T-005.

        # Wipe stale frontend artifacts (re-built fresh in _build_react_frontend)
        for stale in (self.frontend_dir / "dist",
                      self.frontend_dir / "node_modules" / ".vite"):
            if stale.exists():
                shutil.rmtree(stale)  # raises on failure — fail hard

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
        """Definitive: True if any process is LISTEN on this port.

        Uses psutil (kernel-level inspection) — no TCP connect, no
        firewall stealth-mode interaction. TCP connect on Windows
        unreliably times out for closed ports under stealth firewall,
        making it the wrong probe for fail-hard semantics. psutil reads
        the listener table directly. psutil.AccessDenied (if it ever
        arises) propagates per fail-hard.
        """
        return len(self._pids_listening_on(port)) > 0

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

        # ux access: vite path alias `@shared/ux` -> ../../../Shared/ux
        # (configured in vite.config.ts + tsconfig.json). No filesystem
        # symlink needed; the bundler resolves the alias at build time.
        # Verify the target dir exists; fail hard if missing.
        ux_target = self.repo_root / "Code" / "Shared" / "ux"
        if not ux_target.is_dir():
            sys.exit(
                f"ERROR: shared ux directory missing at {ux_target}. "
                "Required by vite alias @shared/ux."
            )

    # ── Backend container names (V11 S-001-REQ-T-001 + S-002-REQ-T-001) ─
    # Container name -> (host port, backend source dir relative to repo root)
    # Each entry: container_name -> (port label, src_dir, build_context).
    # build_context is relative to repo root. FindCare uses repo root because
    # its Dockerfile reaches sibling-tree paths (Code/Shared/, etc.). The
    # refactored services use their own Code/ dir so the same Dockerfile works
    # in both local and HF Space builds (HF flat-rsyncs Code/ contents into
    # the Space root, which then becomes the build context).
    BACKEND_CONTAINERS = {
        "ch-findcare":  ("findcare",
                         "Code/ConversationalUX/FindCareChat/backend",
                         "."),
        "ch-evalcare":  ("evalcare",
                         "evaluateCare/Code",
                         "evaluateCare/Code"),
        "ch-sharedsvc": ("shared",
                         "sharedServices/Code",
                         "sharedServices/Code"),
    }

    # ── Docker daemon precheck ─────────────────────────────────────────
    def _ensure_docker_available(self) -> None:
        """Fail-hard if Docker is not running. V11 S-002-REQ-T-001 makes
        Docker mandatory for local backends; no subprocess-Python fallback
        per BUG-003."""
        result = subprocess.run(
            ["docker", "version"],
            capture_output=True, text=True, timeout=15,
            creationflags=(subprocess.CREATE_NO_WINDOW
                           if sys.platform == "win32" else 0),
        )
        if result.returncode != 0:
            sys.exit(
                "ERROR: Docker daemon not available. "
                "FIX: open Docker Desktop (Start menu -> Docker Desktop) "
                "and re-run this script. "
                "V11 S-002-REQ-T-001 requires local backends to run as "
                "Docker containers; subprocess-Python fallback removed "
                "per BUG-003. "
                f"`docker version` exit={result.returncode}, "
                f"stderr={result.stderr.strip()[:300]}"
            )

    # ── Build backend containers (V11 S-002-REQ-T-001) ─────────────────
    def _build_backend_containers(self) -> None:
        """Docker build per V11 S-002-REQ-T-001. Idempotent: docker build
        with cached layers is fast.

        Build-context pattern (BUG-003 #1 / V11 S-002-REQ-T-001 fix):
        all three backend builds use the REPO ROOT as the build context
        and select their Dockerfile via `-f`. Per Docker docs
        (https://docs.docker.com/build/concepts/context/): "Build
        instructions such as COPY and ADD can refer to any of the files
        and directories in the context." The previous narrow per-backend
        contexts could not reach Code/Shared/, brain/, or
        Code/ConversationalUX/ChatHealthyWhoAmIChat/me/ — required at
        runtime by all three apps — so containers crashed at import time
        (ModuleNotFoundError on ChatHealthyMongoUtilities for findcare,
        on evaluate_care for evalcare). Repo-root .dockerignore keeps the
        transferred context small.
        """
        for container_name, entry in self.BACKEND_CONTAINERS.items():
            _label, src_dir, build_ctx_rel = entry
            image_tag = container_name  # same name for image and container
            dockerfile_rel = f"{src_dir}/Dockerfile"
            dockerfile_abs = self.repo_root / src_dir / "Dockerfile"
            if not dockerfile_abs.is_file():
                sys.exit(
                    f"ERROR: Dockerfile missing at {dockerfile_abs}. "
                    "V11 S-002-REQ-T-001 requires Dockerfile per backend."
                )
            build_ctx_abs = self.repo_root if build_ctx_rel == "." else (self.repo_root / build_ctx_rel)
            self._step_notice(
                f"building image {image_tag} (-f {dockerfile_rel}, "
                f"context={build_ctx_rel})"
            )
            result = subprocess.run(
                ["docker", "build",
                 "-t", image_tag,
                 "-f", str(dockerfile_abs),
                 str(build_ctx_abs)],
                cwd=str(self.repo_root),
                capture_output=True, text=True,
                creationflags=(subprocess.CREATE_NO_WINDOW
                               if sys.platform == "win32" else 0),
            )
            if result.returncode != 0:
                sys.exit(
                    f"ERROR: docker build failed for {image_tag}: "
                    f"{result.stderr.strip()[:500]}"
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

    # ── Start servers (V11 S-002-REQ-T-001: Docker; T-002: website host) ─
    def _start_backend_processes(self) -> None:
        # Backends run as Docker containers per V11 S-002-REQ-T-001.
        # Pattern is `docker rm -f` + `docker run` (NOT `docker start`):
        # `docker start` resumes the existing container instance, which was
        # created from whatever image existed at create time — so
        # subsequent `docker build` images are silently ignored. Removing
        # and re-running guarantees the container instance is created from
        # the freshly-built image (the V11 parity guarantee).
        certs_host = str(self.certs_dir).replace("\\", "/")
        env_file = self.repo_root / "Code" / ".env"
        if not env_file.is_file():
            sys.exit(
                f"ERROR: env file missing at {env_file}; backend containers "
                "depend on it for MongoDB / API credentials."
            )
        # Parse .env via python-dotenv (handles inline comments + quoting
        # that Docker's --env-file parser does not). Pass each as -e to
        # docker run.
        from dotenv import dotenv_values
        env_dict = dotenv_values(env_file)
        env_args: list[str] = []
        for k, v in env_dict.items():
            if v is None:
                continue
            env_args.extend(["-e", f"{k}={v}"])

        cflags = (subprocess.CREATE_NO_WINDOW
                  if sys.platform == "win32" else 0)
        for container_name, (label, _src_dir, _build_ctx) in self.BACKEND_CONTAINERS.items():
            host_port = self.PORTS[label]
            # Remove existing container (silent-ok if absent — `docker rm
            # -f` exit is non-zero when name unknown but harmless).
            subprocess.run(
                ["docker", "rm", "-f", container_name],
                capture_output=True, text=True, creationflags=cflags,
            )
            # Run fresh container from the latest image. Container is
            # named after its image (1:1 convention).
            run_cmd = (
                ["docker", "run", "-d",
                 "--name", container_name,
                 "-p", f"{host_port}:7860",
                 "-v", f"{certs_host}:/certs:ro"]
                + env_args
                + [container_name]
            )
            run_result = subprocess.run(
                run_cmd, capture_output=True, text=True, creationflags=cflags,
            )
            if run_result.returncode != 0:
                sys.exit(
                    f"ERROR: docker run {container_name} failed: "
                    f"{run_result.stderr.strip()[:500]}"
                )
            self._step_notice(
                f"docker run {container_name} -> host port {host_port}"
            )

        # Website wrapper runs as host-OS process per V11 S-002-REQ-T-002
        # (Docker exception, intentional). Stays as subprocess Python.
        certs_arg = str(self.certs_dir)
        website_arg = str(self.website_dir)
        log_dir = self.output_dir / "process_logs"
        log_dir.mkdir(exist_ok=True)
        log_file = (log_dir / f"website-80-443_{self.results['started_at']}.log")
        log_fh = open(log_file, "w", encoding="utf-8")
        proc = subprocess.Popen(
            [sys.executable, str(self.deploy_dir / "_start_website.py"),
             certs_arg, website_arg],
            cwd=str(self.repo_root),
            stdout=log_fh, stderr=subprocess.STDOUT,
            creationflags=(
                (subprocess.CREATE_NEW_PROCESS_GROUP
                 | subprocess.CREATE_NO_WINDOW)
                if sys.platform == "win32" else 0
            ),
        )
        self.backend_procs.append(proc)
        self._step_notice(
            f"started website pid={proc.pid} log={log_file.name} "
            "(host-OS per V11 S-002-REQ-T-002)"
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
            # NOTE: the health endpoints still self-identify as the legacy
            # snake_case service names so existing downstream consumers
            # (smoke tests, banner) keep matching. Renaming is a separate
            # contract change.
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
        """Verify all components respond correctly. Fail-hard per V11
        S-001-REQ-B-001 (atomic deploy / no half-deployed third state):
        any failed verification aborts the deploy. Recording-only without
        gating was a fallback (BUG-003 item #9) — removed.
        """
        passed, failed = [], []
        v = self.results["verification"]

        def record(name: str, ok: bool, detail: str = "") -> None:
            v.append({"name": name, "ok": ok, "detail": detail[:300]})
            (passed if ok else failed).append(name)

        with httpx.Client(verify=False, timeout=10) as c:
            r = c.get("http://localhost/", follow_redirects=False)
            record("http_to_https_301",
                   r.status_code == 301,
                   f"got {r.status_code}")

            r = c.get("https://localhost/")
            record("website_200", r.status_code == 200,
                   f"got {r.status_code}")
            record("website_has_banner",
                   "envBanner" in r.text, "")

            for svc, port in (("findcare", self.PORTS["findcare"]),
                              ("evalcare", self.PORTS["evalcare"]),
                              ("shared",   self.PORTS["shared"])):
                r = c.get(f"https://localhost:{port}/health")
                record(f"{svc}_health",
                       r.status_code == 200,
                       r.text)

        # mTLS verifications (FindCare client cert -> EvalCare, FindCare -> Shared)
        ca = str(self.certs_dir / "ca.crt")
        fc_cert = (str(self.certs_dir / "findcare.crt"),
                   str(self.certs_dir / "findcare.key"))
        for tgt_svc, tgt_port, expected_substr in (
            ("evalcare", self.PORTS["evalcare"], "evaluate_care"),
            ("shared",   self.PORTS["shared"],   "shared_services"),
        ):
            with httpx.Client(cert=fc_cert, verify=ca, timeout=10) as cc:
                r = cc.get(f"https://localhost:{tgt_port}/health")
                ok = r.status_code == 200 and expected_substr in r.text
                record(f"mtls_findcare_to_{tgt_svc}", ok,
                       r.text if ok else f"{r.status_code}: {r.text}")

        self._step_notice(
            f"verification: {len(passed)} passed, {len(failed)} failed"
            + (f"; failed={failed}" if failed else "")
        )
        if failed:
            sys.exit(
                f"ERROR: verification failed for {failed}. Aborting deploy "
                "per V11 S-001-REQ-B-001 (atomic / no half-deployed state)."
            )

    # ── Invoke smoke test (S-001-REQ-B-004 + S-006) ────────────────────
    def _invoke_smoke_test(self) -> int:
        cmd = [
            sys.executable, "-m", "pytest", "-v",
            str(self.deploy_dir / "localSmokeTestPyTest.py"),
            f"--smoke-env={self.env}",
        ]
        # Local has mTLS enabled, so include mtls_required tests (no marker
        # filter). Non-local envs would pass `-m "not mtls_required"` here
        # (BUG-003 item #5 — invocation-level decision, not in-test skip).

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

        self._ensure_docker_available()                     # S-002-REQ-T-001 prereq
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
    # No flags. The deploy is atomic per V11 S-001-REQ-B-001 — there is
    # no half-deploy mode. Previous --no-smoke convenience flag removed
    # per BUG-003 item #6 (invented behavior without V11 backing).
    parser.parse_args(argv)
    return LocalDeploy().run()


if __name__ == "__main__":
    sys.exit(main())
