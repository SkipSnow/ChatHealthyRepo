"""remote_deploy.py - GHA runner deploy manager.

CI-side mirror of local_deploy.py's cloud-deploy half per EPIC-008-F-012-S-001
REQ-T-049. Ships per-target packages from remoteBuild/<target_id>/ to
their cloud destinations. Deploy handlers are imported from local_deploy
(no duplication). The branch checked out IS the version; promote chain
carries the artifact bytes via merge.

Per REQ-T-055: this script MUST execute only on a named GitHub Actions
runner (env var GITHUB_ACTIONS=true).

Secrets: resolved via SecretsResolver bindings sourced from
deployment_architecture.json. In CI the bound store for each (name, env)
pair MUST be a CI-accessible store (gha_secret, hf_space_secret,
cloudflare_env, azure_function_app_setting, azure_automation_variable).
The store handlers other than local_env are stubs today
(NotImplementedError) — wiring them is a follow-up to this commit.

usage (in a GHA workflow step):
    python remote_deploy.py --env qa  --target cloudflare
    python remote_deploy.py --env prod
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection

# Reuse cloud-deploy handlers from local_deploy — same per-target_kind logic.
import local_deploy as ld
from local_build import REMOTE_BUILD_ROOT_REL
from local_deploy import _deploy_one, _select_target_ids


def _step(msg: str) -> None:
    print(f"[remote_deploy] {msg}", flush=True)


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def _require_gha_context() -> None:
    """REQ-T-055 — remote_deploy MUST run only on a named GHA runner."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit(
            "ERROR: remote_deploy.py MUST run only on a GitHub Actions runner "
            "(GITHUB_ACTIONS=true). For workstation deploys use local_deploy.py."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ship per-target build packages from remoteBuild/<target_id>/ to cloud envs (CI)."
    )
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"])
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | 'hf' | 'azure' | a specific target_id.",
    )
    args = parser.parse_args(argv)

    _require_gha_context()
    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} env={args.env} target={args.target}")

    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    env_file = repo_root / "Code" / ".env"

    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    resolver = SecretsResolver.from_collection(coll, env_file=env_file)

    selected = _select_target_ids(coll, args.target)
    if not selected:
        sys.exit(f"ERROR: no targets matched --target={args.target!r}")

    # _deploy_one resolves build_dir against ld._BUILD_ROOT_REL; swap to
    # the remoteBuild path for the duration of this run so the same
    # handler code finds CI's emitted packages.
    original_build_root = ld._BUILD_ROOT_REL
    ld._BUILD_ROOT_REL = REMOTE_BUILD_ROOT_REL
    try:
        by_id = {t.target_id: t for t in coll}
        deployed: list[str] = []
        for target_id, target_kind in selected:
            target = by_id[target_id]
            if args.env not in target.env_binding_set():
                _step(f"  skip {target_id}: no env_binding for {args.env!r}")
                continue
            deployed.append(_deploy_one(
                repo_root, target_id, target_kind, args.env, resolver, coll,
            ))
    finally:
        ld._BUILD_ROOT_REL = original_build_root

    if not deployed:
        sys.exit(
            f"ERROR: nothing deployed for env={args.env!r} target={args.target!r}."
        )
    _step(f"deployed {len(deployed)} target(s):")
    for d in deployed:
        _step(f"  {d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
