"""remote_build.py - GHA runner build manager.

CI-side mirror of local_build.py per EPIC-008-F-012-S-001 REQ-T-049
(symmetric 4-script set). Reads source from the runner's checked-out
tree; emits per-target packages to remote_build/v{N}/<target_id>/.
Build handlers are imported from local_build (no duplication).

Per REQ-T-055: this script MUST execute only on a named GitHub Actions
runner (env var GITHUB_ACTIONS=true). Invocation outside that context
hard-fails.

usage (in a GHA workflow step):
    python architecture/DevOpsBuildDeployAndEnvironmentManagement/remote_build.py --target all
    python architecture/DevOpsBuildDeployAndEnvironmentManagement/remote_build.py --target cloudflare
    python architecture/DevOpsBuildDeployAndEnvironmentManagement/remote_build.py --target target_hf_space_findcare_backend
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

from agile_backlog import AgileBacklogLoader
from crosswalk import Crosswalk
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection

# Reuse build handlers from local_build — same per-target_kind logic.
import local_build as lb
from local_build import (
    REMOTE_BUILD_ROOT_REL,
    _build_one,
    _read_dev_build_number,
    _select_targets,
)


def _step(msg: str) -> None:
    print(f"[remote_build] {msg}", flush=True)


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def _require_gha_context() -> str:
    """REQ-T-055 — remote_build MUST run only on a named GHA runner."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit(
            "ERROR: remote_build.py MUST run only on a GitHub Actions runner "
            "(GITHUB_ACTIONS=true). For workstation builds use local_build.py."
        )
    sha = os.environ.get("GITHUB_SHA", "")
    return sha[:7] if sha else "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build per-target deploy packages under remote_build/v{N}/ on a GHA runner."
    )
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | 'hf' | 'azure' | a specific target_id.",
    )
    args = parser.parse_args(argv)

    build_sha = _require_gha_context()
    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} target={args.target} sha={build_sha}")

    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    backlog_schema = repo_root / "Website" / "schemas" / "ChatHealthyAgileBacklogSchema.json"
    backlog_path = repo_root / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
    env_file = repo_root / "Code" / ".env"

    backlog = AgileBacklogLoader(schema_uri=backlog_schema).load(backlog_path)
    coll: DeploymentCollection = RecordLoader().load_collection(brain_path)
    env_values_for_leak: set[str] = (
        SecretsResolver().env_values_for_leak_check(env_file)
        if env_file.is_file() else set()
    )
    report = Crosswalk().check(
        coll=coll, backlog=backlog, repo_root=repo_root,
        env_values=env_values_for_leak,
    )
    if not report.is_pass:
        sys.stderr.write(report.format() + "\n")
        return report.exit_code()
    _step(f"crosswalk gate passed (targets={len(coll)}, violations=0)")

    build_n = _read_dev_build_number()
    _step(f"build_number={build_n} (dev slot in admin.Versions)")

    targets = _select_targets(coll, args.target)
    if not targets:
        sys.exit(f"ERROR: no targets matched --target={args.target!r}")

    built: list[Path] = []
    for t in targets:
        built.append(_build_one(repo_root, t, build_n, build_sha, build_root_rel=REMOTE_BUILD_ROOT_REL))

    _step(f"built {len(built)} package(s) at v{build_n} under remote_build/:")
    for b in built:
        _step(f"  {b.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
