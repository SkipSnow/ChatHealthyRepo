"""local_build.py - operator's build manager.

Produces a versioned, per-target build package on the local workstation.
Package layout:

    architecture/DevOpsBuildDeployAndEnvironmentManagement/
        local_build/v{N}/<target_id>/
            <materialized deploy bytes for this target>
            manifest.json   # target's slice of deployment_architecture.json
                            # plus build_number and build_sha

N comes from admin.Versions.latest.builds[env=dev].build on the front-end
cluster (auto-incremented on every non-deploy commit by Rule-063). No
secret VALUES are written into any file in the package; only names+stores
appear in manifest.json. local_deploy reads from this tree.

usage:
    python local_build.py --target all
    python local_build.py --target cloudflare
    python local_build.py --target target_cloudflare_pages_website

Per EPIC-008-F-012-S-001. Phase 4a: cloudflare_pages_project only;
HF Spaces and Azure FA still ship via local_publish.py until follow-up
commits subsume them.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from agile_backlog import AgileBacklogLoader
from crosswalk import Crosswalk
from extractor import Extractor
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver
from target_record import DeploymentCollection, TargetRecord
import ch_fonts_inliner
import old_remote_deploy as rd


_BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/local_build")


def _step(msg: str) -> None:
    print(f"[local_build] {msg}", flush=True)


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def _require_clean_working_tree(repo_root: Path) -> str:
    """HEAD SHA pins the source bytes we build. Reject uncommitted source
    changes. Build OUTPUTS under local_build/ are not source and do NOT
    block — they're produced by this very script.
    """
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    build_root_prefix = str(_BUILD_ROOT_REL).replace("\\", "/") + "/"
    offending = []
    for line in r.stdout.splitlines():
        # Porcelain v1 line is "XY path" (or rename "XY orig -> new").
        if not line.strip():
            continue
        path = line[3:]
        # Ignore renames' second path; the porcelain status code already
        # captures the change above.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        path = path.strip('"')
        if path.startswith(build_root_prefix):
            continue
        offending.append(line)
    if offending:
        sys.exit(
            "ERROR: working tree has uncommitted source changes. Commit "
            "them first so HEAD SHA pins the source bytes we build.\n\n"
            + "\n".join(offending)
        )
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    return r.stdout.strip()


def _read_dev_build_number() -> int:
    """Read the current dev build counter from admin.Versions on the
    front-end cluster. Rule-063 auto-increments this on every non-deploy
    commit, so this is the build number for HEAD."""
    from dotenv import load_dotenv
    from pymongo import MongoClient

    repo_root = _find_repo_root(Path(__file__))
    load_dotenv(repo_root / "Code" / ".env")

    conn = os.getenv("MONGO_FRONTEND_connectionString")
    if not conn:
        sys.exit("ERROR: MONGO_FRONTEND_connectionString not set in env or Code/.env")
    coll = MongoClient(conn, serverSelectionTimeoutMS=10000)["admin"]["Versions"]
    latest = coll.find_one(sort=[("from", -1)])
    if latest is None:
        sys.exit("ERROR: admin.Versions has no records. Run seed_versions_collection.py first.")
    for entry in latest.get("builds", []):
        if entry.get("env") == "dev":
            return int(entry["build"])
    sys.exit("ERROR: admin.Versions latest record has no dev slot in builds[].")


def _target_build_dir(repo_root: Path, build_n: int, target_id: str) -> Path:
    return repo_root / _BUILD_ROOT_REL / f"v{build_n}" / target_id


def _write_manifest_snapshot(
    build_dir: Path,
    target: TargetRecord,
    build_n: int,
    build_sha: str,
) -> None:
    """Write the target's slice of deployment_architecture.json into the
    build dir, augmented with build_number + build_sha. Embedded file
    bytes are stripped from `files[]` (they're materialized to disk in
    the same build_dir; carrying them in the manifest would double them).
    """
    files_lean: list[dict] = []
    for f in target.files:
        d = f.to_dict()
        d.pop("embedded_content", None)
        d.pop("layout", None)
        files_lean.append(d)
    snapshot: dict = {
        "$schema": "https://dev.chathealthy.ai/schemas/ChatHealthyBuildPackageManifestSchema.json",
        "build_number": build_n,
        "build_sha": build_sha,
        "target_id": target.target_id,
        "target_kind": target.target_kind,
        "environments": [e.to_dict() for e in target.environments],
        "files": files_lean,
    }
    if target.secrets:
        snapshot["secrets"] = dict(target.secrets)
    (build_dir / "manifest.json").write_text(
        json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _build_cloudflare(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage Website/ into build_dir, inline fonts, materialize managed bytes."""
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    rd._copy_tree(repo_root, build_dir, "Website", ".")
    snippet = ch_fonts_inliner.read_snippet()
    inlined = 0
    for html in build_dir.rglob("*.html"):
        if ch_fonts_inliner.inline_into(html, snippet):
            inlined += 1
    _step(f"  CH_FONTS inlined in {inlined} pages")
    Extractor().materialize(target, build_dir)


def _build_hf_space(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage the HF Space's source set into build_dir, build React frontend
    (FindCare only), materialize managed bytes, copy the Dockerfile to root.

    build_dir contains the docker build context that local_deploy passes
    to `docker build`. Per Skip's framing: build = source bytes for that
    target; deploy does the install procedure (docker build/push, HF API,
    git push to Space repo).
    """
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)

    target_id = target.target_id
    if target_id == "target_hf_space_findcare_backend":
        source_set = rd._findcare_source_set(repo_root)
        # React frontend dist is consumed by the FindCare backend's static/
        # serving path. Build now so the copy below picks up fresh bytes.
        rd._build_react_frontend(repo_root, "dev")
    elif target_id == "target_hf_space_evaluatecare_backend":
        source_set = rd._evaluatecare_source_set(repo_root)
    elif target_id == "target_hf_space_shared_services":
        source_set = rd._sharedservices_source_set(repo_root)
    else:
        raise RuntimeError(f"unknown hf_space target {target_id!r}")

    for src_rel, dst_rel in source_set:
        _step(f"  stage  {src_rel} -> {dst_rel}")
        rd._copy_tree(repo_root, build_dir, src_rel, dst_rel or src_rel)

    rd._write_hf_build_info(build_dir, target_id, "dev")

    if target_id == "target_hf_space_findcare_backend":
        # FindCare React dist -> backend static/.
        dist = (repo_root / "Code" / "ConversationalUX" / "FindCareChat"
                / "frontend" / "dist")
        static_dst = (build_dir / "Code" / "ConversationalUX"
                      / "FindCareChat" / "backend" / "static")
        if static_dst.exists():
            shutil.rmtree(static_dst, ignore_errors=True)
        if not dist.is_dir():
            sys.exit(f"ERROR: React dist missing at {dist}")
        shutil.copytree(dist, static_dst)

    Extractor().materialize(target, build_dir)

    if target_id == "target_hf_space_findcare_backend":
        # FindCare's Dockerfile lives under DevOps/FindCareBackend/; HF
        # Space's docker build context wants it at the root.
        shutil.copy2(
            build_dir / "DevOps" / "FindCareBackend" / "Dockerfile",
            build_dir / "Dockerfile",
        )


_PIPELINE_SOURCE_PREFIX = "pipeline/Code/"
_AZURE_REQUIREMENTS_SRC = "pipeline/Code/requirements-pipeline.txt"
_AZURE_REQUIREMENTS_ZIP_PATH = "requirements.txt"


def _build_azure_function_app(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Materialize the Azure FA deploy.zip from target.files[].

    Each entry in target.files[] is added to the zip; arcname strips the
    `pipeline/Code/` prefix so Azure Functions sees function_app.py +
    host.json at the zip root. `requirements-pipeline.txt` is renamed to
    `requirements.txt` (Azure's lookup name).
    """
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    zip_path = build_dir / "deploy.zip"
    _step(f"  building deploy.zip from {len(target.files)} JSON-declared files")
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in target.files:
            src_path = repo_root / f.source_location
            if not src_path.is_file():
                sys.exit(
                    f"ERROR: file in JSON manifest not present on disk: "
                    f"{f.source_location}"
                )
            if f.source_location == _AZURE_REQUIREMENTS_SRC:
                arcname = _AZURE_REQUIREMENTS_ZIP_PATH
            elif f.source_location.startswith(_PIPELINE_SOURCE_PREFIX):
                arcname = f.source_location[len(_PIPELINE_SOURCE_PREFIX):]
            else:
                sys.exit(
                    f"ERROR: azure target file {f.source_location!r} does "
                    f"not start with {_PIPELINE_SOURCE_PREFIX!r}; cannot "
                    f"map to a zip arcname."
                )
            zf.write(src_path, arcname=arcname)
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    _step(f"  zip built: {zip_path.name} ({size_mb:.1f} MB, {len(target.files)} entries)")


def _build_one(repo_root: Path, target: TargetRecord, build_n: int, build_sha: str) -> Path:
    build_dir = _target_build_dir(repo_root, build_n, target.target_id)
    _step(f"=== {target.target_kind} {target.target_id} -> {build_dir} ===")
    if target.target_kind == "cloudflare_pages_project":
        _build_cloudflare(repo_root, target, build_dir)
    elif target.target_kind == "hf_space":
        _build_hf_space(repo_root, target, build_dir)
    elif target.target_kind == "azure_function_app":
        _build_azure_function_app(repo_root, target, build_dir)
    else:
        raise RuntimeError(
            f"target_kind {target.target_kind!r} not supported in local_build."
        )
    _write_manifest_snapshot(build_dir, target, build_n, build_sha)
    return build_dir


_BUILDABLE_KINDS = ("cloudflare_pages_project", "hf_space", "azure_function_app")


def _select_targets(coll: DeploymentCollection, target_arg: str) -> list[TargetRecord]:
    if target_arg == "all":
        # Phase 4b scope: cloudflare + hf. Azure still routed through old_local_publish.
        return [t for t in coll if t.target_kind in _BUILDABLE_KINDS]
    if target_arg in ("cloudflare", "cloudflare_pages_project"):
        return [t for t in coll if t.target_kind == "cloudflare_pages_project"]
    if target_arg in ("hf", "hf_space"):
        return [t for t in coll if t.target_kind == "hf_space"]
    if target_arg in ("azure", "azure_function_app"):
        return [t for t in coll if t.target_kind == "azure_function_app"]
    # Exact target_id match
    for t in coll:
        if t.target_id == target_arg:
            return [t]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build per-target deploy packages under local_build/v{N}/."
    )
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | a specific target_id. Default: all (Phase 4a = cloudflare only).",
    )
    args = parser.parse_args(argv)

    repo_root = _find_repo_root(Path(__file__))
    _step(f"repo_root={repo_root} target={args.target}")

    build_sha = _require_clean_working_tree(repo_root)
    _step(f"HEAD={build_sha} (clean tree)")

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
        built.append(_build_one(repo_root, t, build_n, build_sha))

    _step(f"built {len(built)} package(s) at v{build_n}:")
    for b in built:
        _step(f"  {b.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
