# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""One-command deploy orchestrator.

Spawns local_deploy.py as a non-interactive subprocess with
CHATHEALTHY_DEPLOY_AUTHORIZED=1 so every prompt is pre-authorized, then
exits with the deploy's return code.

  python automated_deploy.py

Commit / push is a SEPARATE concern, not part of this script. The
operator decides when to check in code and runs git from their terminal
when ready. This script only deploys + reports.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPO_ROOT_ENV = "CHATHEALTHY_PROJECT_ROOT"


def _resolve_repo_root() -> Path:
    if REPO_ROOT_ENV not in os.environ:
        sys.exit(f"ERROR: {REPO_ROOT_ENV} env var not set. Cannot resolve paths.")
    return Path(os.environ[REPO_ROOT_ENV]).resolve()


def _run_deploy(repo_root: Path) -> int:
    deploy_dir = repo_root / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement"
    deploy_script = deploy_dir / "local_deploy.py"
    if not deploy_script.is_file():
        sys.exit(f"ERROR: local_deploy.py not found at {deploy_script}")

    env = os.environ.copy()
    env["CHATHEALTHY_DEPLOY_AUTHORIZED"] = "1"
    env.setdefault("CHATHEALTHY_PROJECT_ROOT", str(repo_root))

    print(f"[orch] running {deploy_script.name} (non-interactive)", flush=True)
    proc = subprocess.Popen(
        [sys.executable, str(deploy_script)],
        cwd=str(repo_root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=1,
        text=True,
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    return proc.wait()


def main() -> int:
    repo_root = _resolve_repo_root()
    rc = _run_deploy(repo_root)
    print(f"[orch] deploy exit rc={rc}", flush=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
