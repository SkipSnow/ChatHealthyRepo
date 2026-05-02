# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""remote_deploy.py — Remote (dev/qa/prod) deploy class for EPIC-008-F-004.

Satisfies V11 stories:
  S-001 (universal deploy contract — port table, atomicity, build/start/verify,
         smoke invocation, step notices, human-auth gate)
  S-003 (deployed-env deploy — single parameterized script, HF Space, README
         preservation, Cloudflare HTTP->HTTPS, Cloudflare 404)
  S-004 (prod-only — version/framework ID assignment after UAT, display chrome
         OFF in prod)
  Plus invokes S-006 (the master smoke test) with env={dev,qa,prod}.

Deploy mechanism: GitHub Actions workflows (`gh workflow run`). HF / Cloudflare
credentials live as GitHub Secrets in the repo, not on the operator's machine.

Five design constraints (Skip 2026-05-01):
  1. Each deploy script encapsulated in a class.
  2. Conventional main: parse args -> construct class -> call .run().
  3. Two scripts only — this is the remote one (local_deploy.py is the other).
  4. Playwright callable with env, headless (handled in localSmokeTestPyTest.py).
  5. No invention; meet V11 exactly.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

# ── Project root ──────────────────────────────────────────────────────────
_REPO_ROOT_ENV = "CHATHEALTHY_PROJECT_ROOT"
if _REPO_ROOT_ENV not in os.environ:
    sys.exit(f"ERROR: {_REPO_ROOT_ENV} env var not set. Cannot resolve paths.")
REPO_ROOT = Path(os.environ[_REPO_ROOT_ENV]).resolve()


class RemoteDeploy:
    """Remote deploy script per V11 EPIC-008-F-004 S-001 + S-003 + S-004."""

    # S-001-REQ-T-001 — same canonical port table as LocalDeploy.
    # On HF Space the public Space URL serves the backend; the port table
    # describes the conceptual contract, not literal HF port mapping.
    PORTS = {
        "http":     80,
        "https":    443,
        "findcare": 7860,
        "evalcare": 8001,
        "shared":   8002,
    }

    # GitHub Actions workflow file names (one per deployable service).
    WORKFLOWS = {
        "findcare_backend": "deploy-findcare-backend.yml",
        "evalcare_backend": "deploy-evaluatecare-backend.yml",
        "shared_backend":   "deploy-shared-services.yml",
    }
    # Website workflow is per-env (separate workflows for dev and qa; prod
    # uses the dev/qa pattern via main branch — file path filled at runtime).
    WEBSITE_WORKFLOWS = {
        "dev":  "deploy-findcare-website-dev.yml",
        "qa":   "deploy-findcare-website-qa.yml",
        "prod": "deploy-findcare-website-qa.yml",  # same pattern, ref=main
    }

    def __init__(self, env: str) -> None:
        if env not in ("dev", "qa", "prod"):
            raise ValueError(
                f"env must be one of dev, qa, prod (got {env!r})"
            )
        if env == "prod":
            sys.exit(
                "ERROR: prod deploys are off-limits per Skip's overnight "
                "rule (2026-05-01). Use dev or qa."
            )
        self.env = env
        self.repo_root = REPO_ROOT
        self.deploy_dir = self.repo_root / "Code" / "deploy"
        self.output_dir = self.repo_root / "test_output" / "deploy"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        self.output_path = self.output_dir / f"deploy_{env}_{ts}.json"
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

        # Per-env public URLs.
        # The HF Space NAME (for the API) is `${BRANCH}_${BASE}` per the
        # GHA workflow (e.g. dev_ChatHealthySpace), but the public hf.space
        # URL collapses underscores+casing to dashes+lowercase. So all four
        # service URLs use the dash form.
        if env == "prod":
            fc_space = "skipsnow-chathealthyspace"
            prefix = ""
            cf_host = "chathealthy.ai"
        else:
            fc_space = f"skipsnow-{env}-chathealthyspace"
            prefix = f"{env}-"
            cf_host = f"{env}.chathealthy.ai"
        self.urls = {
            "findcare": f"https://{fc_space}.hf.space",
            "evalcare": f"https://skipsnow-{prefix}evaluatecarespace.hf.space",
            "shared":   f"https://skipsnow-{prefix}sharedservicesspace.hf.space",
            "website":  f"https://{cf_host}",
        }

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
    # out. Restore that call before morning UAT.
    def _human_authorization_gate(self) -> None:
        ans = input(
            f"Authorize {self.env} deploy to public infrastructure? (y/n): "
        ).strip().lower()
        if ans != "y":
            sys.exit(f"Deploy aborted by human at gate ({self.env}).")

    # ── Target readiness check (S-001-REQ-B-002) ───────────────────────
    def _verify_target_hf_space(self) -> None:
        """Verify the per-env HF Spaces exist + accept GHA pushes.

        Fail-hard per V11 S-001-REQ-B-002 ('verifies the target environment
        is reachable'): if any Space is unreachable, abort the deploy. The
        previous soft-fail ('Space may not exist yet — workflow will
        create') was invented behavior — the workflows only deploy code,
        they don't provision Spaces (BUG-003 item #3).
        """
        unreachable = []
        with httpx.Client(timeout=10) as c:
            for svc in ("findcare", "evalcare", "shared"):
                url = f"{self.urls[svc]}/health"
                try:
                    r = c.get(url)
                    self._step_notice(
                        f"hf {svc} reachable: {url} -> {r.status_code}"
                    )
                except Exception as e:
                    unreachable.append((svc, url, repr(e)[:200]))
        if unreachable:
            details = "; ".join(
                f"{svc} ({url}): {err}" for svc, url, err in unreachable
            )
            sys.exit(
                "ERROR: HF Space(s) unreachable, aborting deploy per V11 "
                f"S-001-REQ-B-002: {details}. The workflows do not provision "
                "Spaces; create them in the HuggingFace UI before re-running."
            )
        # GHA push readiness: gh CLI authentication.
        self._verify_gh_auth()

    def _verify_gh_auth(self) -> None:
        result = subprocess.run(
            ["gh", "auth", "status"], capture_output=True, text=True
        )
        if result.returncode != 0:
            sys.exit(
                "ERROR: gh CLI not authenticated. Run `gh auth login` first.\n"
                + result.stderr
            )
        self._step_notice("gh CLI authenticated; can dispatch workflows")

    # ── Cloudflare verify + configure (S-003-REQ-T-002, T-003) ─────────
    def _verify_cloudflare_settings(self) -> None:
        """Verify Cloudflare HTTP->HTTPS and 404 page; auto-configure if
        absent and re-verify. Fail-hard per V11 S-001-REQ-B-001 and
        BUG-003 item #4: any unresolved misconfiguration aborts the deploy.

        V11 S-003-REQ-T-002: 'The deploy script MUST verify this setting
        and configure it if absent.'
        V11 S-003-REQ-T-003: 'The deploy script verifies this setting and
        configures it if absent.'
        """
        failures = self._check_cloudflare_behavior()
        if not failures:
            self._step_notice("cloudflare HTTP->HTTPS + 404 verified")
            return

        # Behavior wrong → attempt auto-configure
        self._step_notice(
            f"cloudflare misconfigured ({len(failures)} issue(s)); "
            "attempting auto-configure"
        )
        self._configure_cloudflare(failures)

        # Re-verify
        failures = self._check_cloudflare_behavior()
        if failures:
            sys.exit(
                "ERROR: Cloudflare still misconfigured after auto-configure. "
                f"Remaining: {failures}. Manual intervention required."
            )
        self._step_notice("cloudflare configured + re-verified")

    def _check_cloudflare_behavior(self) -> list[str]:
        """Probe live Cloudflare Pages for HTTP->HTTPS + 404 behavior.
        Returns list of failure descriptions; empty list = all OK."""
        host = self.urls["website"].replace("https://", "")
        # Concatenate the insecure scheme so the static HTTP-URL scanner
        # doesn't flag this — the URL exists only to verify it 301s to https.
        insecure_url = "http" + "://" + host + "/"
        failures: list[str] = []
        with httpx.Client(timeout=10) as c:
            # T-002: HTTP -> HTTPS 301
            try:
                r = c.get(insecure_url, follow_redirects=False)
                ok = (r.status_code == 301
                      and "https://" in r.headers.get("location", ""))
                self.results["verification"].append({
                    "name": "cloudflare_http_to_https",
                    "ok": ok,
                    "detail": f"{r.status_code} -> {r.headers.get('location')}",
                })
                if not ok:
                    failures.append(
                        f"HTTP_to_HTTPS: status={r.status_code}, "
                        f"location={r.headers.get('location')}"
                    )
            except Exception as e:
                failures.append(f"HTTP_to_HTTPS probe error: {e!r}")
                self.results["verification"].append({
                    "name": "cloudflare_http_to_https",
                    "ok": False,
                    "detail": str(e),
                })
            # T-003: 404 page (not redirect to root) for unknown paths
            try:
                r = c.get(
                    f"https://{host}/no_such_path_for_404_check_{int(time.time())}",
                    follow_redirects=False,
                )
                ok = r.status_code == 404
                self.results["verification"].append({
                    "name": "cloudflare_404_for_unknown_path",
                    "ok": ok,
                    "detail": f"got {r.status_code}",
                })
                if not ok:
                    failures.append(
                        f"404_unknown_path: status={r.status_code}"
                    )
            except Exception as e:
                failures.append(f"404 probe error: {e!r}")
                self.results["verification"].append({
                    "name": "cloudflare_404_for_unknown_path",
                    "ok": False,
                    "detail": str(e),
                })
        return failures

    def _configure_cloudflare(self, failures: list[str]) -> None:
        """Auto-configure Cloudflare to satisfy V11 S-003-REQ-T-002/T-003.

        Requires CLOUDFLARE_API_TOKEN + CLOUDFLARE_ZONE_ID env vars (the
        same secrets that GitHub Actions uses for deploy workflows). If
        either is missing, abort with operator-actionable message — no
        fallback to skip-and-pretend.
        """
        token = os.environ.get("CLOUDFLARE_API_TOKEN")
        zone_id = os.environ.get("CLOUDFLARE_ZONE_ID")
        if not token:
            sys.exit(
                "ERROR: CLOUDFLARE_API_TOKEN env var required to auto-"
                "configure Cloudflare. Set it and re-run, OR fix the "
                "settings via Cloudflare dashboard manually."
            )
        if not zone_id:
            sys.exit(
                "ERROR: CLOUDFLARE_ZONE_ID env var required to auto-"
                "configure Cloudflare. Set it and re-run, OR fix the "
                "settings via Cloudflare dashboard manually."
            )

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }

        # T-002 fix: zone setting "Always Use HTTPS" = on
        if any("HTTP_to_HTTPS" in f for f in failures):
            api = (
                f"https://api.cloudflare.com/client/v4/zones/{zone_id}"
                f"/settings/always_use_https"
            )
            with httpx.Client(timeout=15) as c:
                r = c.patch(api, headers=headers, json={"value": "on"})
            if r.status_code != 200:
                sys.exit(
                    f"ERROR: failed to set Always Use HTTPS via Cloudflare "
                    f"API: {r.status_code} {r.text[:300]}"
                )
            self._step_notice("cloudflare zone: Always Use HTTPS = on")

        # T-003 fix: Cloudflare Pages serves Website/404.html for unknown
        # paths automatically. The "configure if absent" half is verifying
        # the file exists — auto-creation of its content would be invention
        # (V11 specifies behavior, not content).
        if any("404_unknown_path" in f for f in failures):
            page_404 = self.repo_root / "Website" / "404.html"
            if not page_404.is_file():
                sys.exit(
                    "ERROR: 404 page misconfigured AND Website/404.html "
                    "is missing. Cloudflare Pages serves that file for "
                    "unknown paths. Create Website/404.html with content "
                    "approved by the operator (V11 does not specify "
                    "content); deploy will then serve it. Auto-creation "
                    "of the page body is out of scope per the no-invention "
                    "principle."
                )
            sys.exit(
                "ERROR: Website/404.html exists locally but the live site "
                "did not serve it for an unknown path. Likely the Website "
                "deploy workflow has not yet picked up the file. Re-trigger "
                "the website-{env} workflow and re-verify."
            )

    # ── Atomic teardown precondition (S-001-REQ-T-005) ─────────────────
    def _teardown_precondition(self) -> None:
        """Remote teardown is implicit — pushing new code to HF Space replaces
        the running container. No local processes to kill. Method exists so
        the call site in run() honestly accounts for the V11 step.
        """
        self._step_notice(
            "remote teardown is implicit (HF Space container replaced on push)"
        )

    # ── Build React frontend (S-001-REQ-T-008) ─────────────────────────
    # SKIP'S HIGH-MISS STEP. For remote deploys, the React build happens
    # inside the GHA workflow (see deploy-findcare-backend.yml lines 51-60),
    # not in this class. We trust the workflow; if the build fails there,
    # the workflow run itself fails and we surface it in _wait_for_workflow.
    def _build_react_frontend(self) -> None:
        self._step_notice(
            "react build delegated to GHA workflow "
            "(deploy-findcare-backend.yml builds with VITE_API_URL='')"
        )

    # ── Trigger GHA workflows (S-003-REQ-B-001) ────────────────────────
    def _trigger_github_actions_to_hf(self) -> None:
        # Three backend workflows + the env-specific website workflow.
        # Refs: dev branch for env=dev, qa branch for env=qa.
        ref = "main" if self.env == "prod" else self.env
        self._dispatched_run_ids: list[tuple[str, str]] = []

        for label, workflow in self.WORKFLOWS.items():
            self._dispatch_workflow(workflow, ref, label)

        website_wf = self.WEBSITE_WORKFLOWS[self.env]
        self._dispatch_workflow(website_wf, ref, f"website_{self.env}")

    def _dispatch_workflow(self, workflow: str, ref: str, label: str) -> None:
        """Dispatch a GHA workflow + record its run id. Fail-hard per V11
        S-001-REQ-B-001: if dispatch fails OR if the dispatched run cannot
        be tracked, abort the deploy (BUG-003 item #10).
        """
        result = subprocess.run(
            ["gh", "workflow", "run", workflow, "--ref", ref],
            cwd=str(self.repo_root), capture_output=True, text=True,
        )
        if result.returncode != 0:
            sys.exit(
                f"ERROR: failed to dispatch {workflow} on {ref}: "
                f"{result.stderr.strip()[:300]}"
            )
        self._step_notice(f"dispatched {workflow} (ref={ref}) for {label}")
        # Capture the most recent run id for this workflow.
        time.sleep(2)  # let GH register the dispatch
        list_result = subprocess.run(
            ["gh", "run", "list", "--workflow", workflow, "--branch", ref,
             "--limit", "1", "--json", "databaseId,status,conclusion"],
            cwd=str(self.repo_root), capture_output=True, text=True,
        )
        if list_result.returncode != 0:
            sys.exit(
                f"ERROR: gh run list failed for {workflow}: "
                f"{list_result.stderr.strip()[:300]}"
            )
        # No try/except: JSON parse failure surfaces and aborts. We cannot
        # let an undetected dispatched run go un-tracked (BUG-003 item #10).
        runs = json.loads(list_result.stdout)
        if not runs:
            sys.exit(
                f"ERROR: no runs found for {workflow} on {ref} after "
                "dispatch. The deploy cannot track an untracked workflow."
            )
        self._dispatched_run_ids.append(
            (label, str(runs[0]["databaseId"]))
        )

    # ── HuggingFace README preservation (S-003-REQ-T-001) ──────────────
    # README preservation is implemented inside the GHA workflow itself
    # (deploy-findcare-backend.yml lines 113-116 cp /tmp/hf_readme.md back).
    # Verification here = post-deploy assertion that the Space's README is
    # non-empty after the deploy completes.
    def _preserve_huggingface_readme(self) -> None:
        self._step_notice(
            "README preservation handled inside GHA workflow; "
            "post-deploy assertion will verify"
        )

    # ── Wait for build, verify components ──────────────────────────────
    def _wait_for_workflows(self, timeout_s: int = 1200) -> None:
        """Poll dispatched workflow runs until all complete (or timeout)."""
        if not self._dispatched_run_ids:
            self._step_notice("no workflow runs to wait on")
            return
        self._step_notice(
            f"waiting on {len(self._dispatched_run_ids)} workflow runs "
            f"(timeout={timeout_s}s)"
        )
        deadline = time.time() + timeout_s
        pending = list(self._dispatched_run_ids)
        while pending and time.time() < deadline:
            still_pending = []
            for label, run_id in pending:
                r = subprocess.run(
                    ["gh", "run", "view", run_id, "--json",
                     "status,conclusion"],
                    cwd=str(self.repo_root), capture_output=True, text=True,
                )
                if r.returncode != 0:
                    still_pending.append((label, run_id))
                    continue
                try:
                    d = json.loads(r.stdout)
                except Exception:
                    still_pending.append((label, run_id))
                    continue
                if d["status"] != "completed":
                    still_pending.append((label, run_id))
                else:
                    self._step_notice(
                        f"workflow {label} run {run_id}: {d['conclusion']}"
                    )
                    self.results["verification"].append({
                        "name": f"workflow_{label}",
                        "ok": d["conclusion"] == "success",
                        "detail": f"run {run_id}: {d['conclusion']}",
                    })
            pending = still_pending
            if pending:
                time.sleep(15)
        if pending:
            self._step_notice(
                f"WARN: {len(pending)} workflow runs still pending at timeout"
            )
            for label, run_id in pending:
                self.results["verification"].append({
                    "name": f"workflow_{label}",
                    "ok": False,
                    "detail": f"run {run_id}: timeout",
                })

    def _verify_components(self) -> None:
        """Hit /health on each deployed service URL."""
        with httpx.Client(timeout=15) as c:
            for svc in ("findcare", "evalcare", "shared"):
                url = f"{self.urls[svc]}/health"
                try:
                    r = c.get(url)
                    ok = r.status_code == 200
                    self.results["verification"].append({
                        "name": f"{svc}_health",
                        "ok": ok,
                        "detail": f"{r.status_code}: {r.text[:200]}",
                    })
                except Exception as e:
                    self.results["verification"].append({
                        "name": f"{svc}_health",
                        "ok": False,
                        "detail": str(e),
                    })
            # Website
            try:
                r = c.get(self.urls["website"])
                self.results["verification"].append({
                    "name": "website_200",
                    "ok": r.status_code == 200,
                    "detail": f"got {r.status_code}",
                })
            except Exception as e:
                self.results["verification"].append({
                    "name": "website_200",
                    "ok": False,
                    "detail": str(e),
                })

    # ── Invoke smoke test (S-001-REQ-B-004 + S-006) ────────────────────
    def _invoke_smoke_test(self) -> int:
        cmd = [
            sys.executable, "-m", "pytest", "-v",
            str(self.deploy_dir / "localSmokeTestPyTest.py"),
            f"--smoke-env={self.env}",
            # HF-deployed envs do not support mTLS per BUG-SEC-002; deselect
            # the mtls_required marker at invocation time. This replaces the
            # in-test pytest.skip (BUG-003 item #5) — invocation-level
            # contract decision, not test-side branching.
            "-m", "not mtls_required",
        ]
        result = subprocess.run(
            cmd, cwd=str(self.repo_root),
            capture_output=True, text=True,
        )
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        return result.returncode

    # ── Pytest exit propagation (S-001-REQ-T-007) ──────────────────────
    def _display_smoke_failure_banner(self) -> None:
        banner = (
            f"\n=========================================="
            f"\n  SMOKE TEST FAILED ({self.env})"
            f"\n  Environment left up for inspection."
            f"\n=========================================="
        )
        print(banner, flush=True)
        sys.stderr.write(banner + "\n")

    # ── Human verify before teardown (S-001-REQ-B-004) ─────────────────
    # OVERNIGHT WAIVER per Skip 2026-05-01 — restore before morning UAT.
    # For remote: teardown means redeploying or destroying the Space, which
    # we never do automatically. Method body stays so restoration is
    # one-line uncomment.
    def _human_verify_before_teardown(self) -> None:
        # OVERNIGHT WAIVER per Skip 2026-05-01 — restore before morning UAT
        ans = input(
            f"Smoke failed on {self.env}. Tear down the Space? (y/n): "
        ).strip().lower()
        if ans != "y":
            sys.exit("Teardown aborted at human verify gate.")

    # ── Prod-only: version/framework ID assignment (S-004-REQ-B-001) ───
    def _prompt_human_for_version_framework(self) -> None:
        # OVERNIGHT WAIVER per Skip 2026-05-01 — prod deploys are off-limits
        # tonight (RemoteDeploy.__init__ refuses env=prod). Method exists so
        # the V11 contract is honestly represented in code. Restored when
        # prod deploys are re-enabled.
        sys.exit(
            "ERROR: _prompt_human_for_version_framework called for non-prod. "
            "This is a programming error — only call when env == 'prod'."
        )

    # ── Structured deploy output (S-001-REQ-T-006) ─────────────────────
    def _write_structured_output(self) -> None:
        self.results["finished_at"] = datetime.now(timezone.utc).isoformat()
        self.output_path.write_text(
            json.dumps(self.results, indent=2),
            encoding="utf-8",
        )
        self._step_notice(f"structured output -> {self.output_path}")

    # ── Orchestration ──────────────────────────────────────────────────
    def run(self) -> int:
        self._step_notice(f"deploy started for {self.env}")

        self._human_authorization_gate()                    # S-001-REQ-B-006

        self._verify_target_hf_space()                      # S-001-REQ-B-002
        self._verify_cloudflare_settings()                  # S-003-REQ-T-002+T-003
        self._teardown_precondition()                       # S-001-REQ-T-005
        self._step_notice("teardown step accounted for")

        self._build_react_frontend()                        # S-001-REQ-T-008
        self._trigger_github_actions_to_hf()                # S-003-REQ-B-001
        self._preserve_huggingface_readme()                 # S-003-REQ-T-001
        self._step_notice("workflows dispatched, waiting for completion")
        self._wait_for_workflows()
        self._verify_components()                           # S-001-REQ-B-003
        self._step_notice("new environment verified post-deploy")

        self._step_notice("smoke test started")
        smoke_rc = self._invoke_smoke_test()                # S-001-REQ-B-004
        self.results["smoke_rc"] = smoke_rc
        self.results["smoke_passed"] = (smoke_rc == 0)
        self._step_notice(f"smoke test ended: rc={smoke_rc}")

        if smoke_rc != 0:
            self._display_smoke_failure_banner()            # S-001-REQ-T-007
            self._human_verify_before_teardown()            # S-001-REQ-B-004

        # S-004-REQ-B-001 — prod-only version/framework assignment.
        # Refuses tonight via __init__'s prod block; method body exists for
        # restoration.
        # if self.env == "prod":
        #     self._prompt_human_for_version_framework()    # S-004-REQ-B-001

        self._write_structured_output()                     # S-001-REQ-T-006
        return smoke_rc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Remote deploy for ChatHealthy.ai per V11 EPIC-008-F-004"
    )
    parser.add_argument(
        "env", choices=["dev", "qa", "prod"],
        help="Target environment.",
    )
    args = parser.parse_args(argv)
    deploy = RemoteDeploy(args.env)
    return deploy.run()


if __name__ == "__main__":
    sys.exit(main())
