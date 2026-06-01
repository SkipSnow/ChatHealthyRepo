"""local_build.py - operator's build manager.

Produces the current per-target build package on the local workstation.
Package layout:

    architecture/DevOpsBuildDeployAndEnvironmentManagement/
        localBuild/<target_id>/
            <materialized deploy bytes for this target>
            manifest.json   # target's slice of deployment_architecture.json
                            # plus build_number and build_sha

Only the current build lives on disk; localBuild/ is gitignored. The
build_number comes from admin.Versions.latest.builds[env=dev].build on
the front-end cluster (auto-incremented on every non-deploy commit by
Rule-063) and is stamped into manifest.json. No secret VALUES are
written into any file in the package; only names+stores appear in
manifest.json. local_deploy reads from this tree.

usage:
    python local_build.py --target all
    python local_build.py --target cloudflare
    python local_build.py --target target_cloudflare_pages_website

Per EPIC-008-F-012-S-001.
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
import aca_helpers
import ch_fonts_inliner
import hf_helpers as rd


_BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/localBuild")
REMOTE_BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/remoteBuild")


def _step(msg: str) -> None:
    print(f"[local_build] {msg}", flush=True)


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {start}")


def _require_local_context() -> None:
    """REQ-T-055 — local_build MUST run only on an operator's workstation,
    never on a GitHub Actions runner. For CI builds use remote_build.py."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.exit(
            "ERROR: local_build.py MUST NOT run on a GitHub Actions runner. "
            "Use remote_build.py instead (REQ-T-055)."
        )


def _require_clean_working_tree(repo_root: Path) -> str:
    """HEAD SHA pins the source bytes we build. Reject uncommitted source
    changes. localBuild/ is gitignored so changes there never appear in
    git status; remoteBuild/ IS tracked and uncommitted changes there
    DO block (they belong to the operator to commit or revert).
    """
    r = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    )
    build_root_prefix = str(_BUILD_ROOT_REL).replace("\\", "/") + "/"
    offending = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        path = line[3:]
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


def _target_build_dir(repo_root: Path, build_n: int, target_id: str, build_root_rel: Path | None = None) -> Path:
    root = build_root_rel if build_root_rel is not None else _BUILD_ROOT_REL
    return repo_root / root / target_id


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


_GATEWAY_SOURCE_PREFIX = "pipeline/"
_GATEWAY_HOST_JSON = """{
  "version": "2.0",
  "functionTimeout": "00:03:00",
  "logging": {
    "logLevel": {
      "default": "Information",
      "Host.Results": "Error",
      "Function": "Information",
      "Azure.Core": "Warning",
      "Azure.Storage": "Warning"
    },
    "applicationInsights": {
      "samplingSettings": {
        "isEnabled": true,
        "maxTelemetryItemsPerSecond": 20,
        "excludedTypes": "Request;Exception"
      }
    }
  },
  "extensionBundle": {
    "id": "Microsoft.Azure.Functions.ExtensionBundle",
    "version": "[4.*, 5.0.0)"
  }
}
"""
_GATEWAY_REQUIREMENTS_TXT = """azure-functions
pymongo
"""

# Python v2 worker hard-codes `function_app.py` as the entry-point filename
# and no Azure-side setting overrides this. The source file in the repo
# keeps its descriptive name (ChatHealthyDataPipelinesGatewayFunctionApp.py);
# this stub is generated INTO THE ZIP at build time only and re-exports
# the `app` symbol the v2 worker expects to find.
_GATEWAY_FUNCTION_APP_STUB = """from ChatHealthyDataPipelinesGatewayFunctionApp import app  # noqa: F401
"""


def _build_azure_function_app(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Materialize the Azure FA deploy.zip from target.files[].

    Two file-path conventions are supported:
      - pipeline/Code/<file>       worker-tree files (legacy ACA mirror).
      - pipeline/<file>            top-level pipeline files (the gateway).
                                   Arcname strips the `pipeline/` prefix only.

    `requirements-pipeline.txt` (if listed) is renamed to `requirements.txt`.

    Gateway target (single .py source under pipeline/, no host.json, no
    requirements.txt in the manifest) has its host.json + requirements.txt
    generated into the zip at build time so the source manifest stays a
    single file per the Gateway directive.
    """
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    zip_path = build_dir / "deploy.zip"
    _step(f"  building deploy.zip from {len(target.files)} JSON-declared files")
    arcnames_written: set[str] = set()

    # The gateway runs on Azure Functions Linux Consumption with
    # WEBSITE_RUN_FROM_PACKAGE; the runtime does NOT pip install at
    # cold-start, so dependencies have to be baked into the zip under
    # .python_packages/lib/site-packages/ (the layout the Python worker
    # auto-discovers). Pre-install them here, cross-platform, targeting
    # Linux manylinux2014 + python 3.11 so wheels resolve correctly even
    # from a Windows operator host. Skipped for ACA targets (those build
    # a Docker image; pip runs inside the image build).
    site_packages = build_dir / ".python_packages" / "lib" / "site-packages"
    site_packages.mkdir(parents=True, exist_ok=True)
    pip_cmd = [
        sys.executable, "-m", "pip", "install",
        "--target", str(site_packages),
        "--platform", "manylinux2014_x86_64",
        "--python-version", "311",
        "--only-binary=:all:",
        "--implementation", "cp",
        "--abi", "cp311",
        "--no-deps",
        "pymongo",
        "dnspython",
    ]
    r = subprocess.run(pip_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(
            f"ERROR: pip install for gateway zip failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )

    # Linux Consumption mounts the deploy package as wwwroot. zipfile on
    # Windows writes external_attr=0, which extracts as Unix mode 0000 on
    # the Linux host — the runtime then 503s with "Permission denied" on
    # host.json. Force every entry to mode 0644 (regular file flag 0x8000
    # | rw-r--r--) so the Functions host can read everything it deploys.
    _UNIX_FILE_0644 = (0o100644 << 16)

    def _add_file(zf, arcname: str, content: bytes) -> None:
        info = zipfile.ZipInfo(arcname)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = _UNIX_FILE_0644
        zf.writestr(info, content)

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
            elif f.source_location.startswith(_GATEWAY_SOURCE_PREFIX):
                arcname = f.source_location[len(_GATEWAY_SOURCE_PREFIX):]
            else:
                sys.exit(
                    f"ERROR: azure target file {f.source_location!r} does "
                    f"not start with {_PIPELINE_SOURCE_PREFIX!r} or "
                    f"{_GATEWAY_SOURCE_PREFIX!r}; cannot map to a zip arcname."
                )
            _add_file(zf, arcname, src_path.read_bytes())
            arcnames_written.add(arcname)
        # Gateway shape: single .py + no host.json/requirements.txt in the
        # source manifest. Synthesize them so the deployable zip has the
        # minimum scaffolding Azure Functions Python needs.
        if "host.json" not in arcnames_written:
            _add_file(zf, "host.json", _GATEWAY_HOST_JSON.encode("utf-8"))
        if "requirements.txt" not in arcnames_written:
            _add_file(zf, "requirements.txt", _GATEWAY_REQUIREMENTS_TXT.encode("utf-8"))
        # v2 worker entry-point stub — see _GATEWAY_FUNCTION_APP_STUB doc.
        if "function_app.py" not in arcnames_written:
            _add_file(zf, "function_app.py", _GATEWAY_FUNCTION_APP_STUB.encode("utf-8"))
        # Bundle the pip-installed dependencies. Linux Functions Python
        # auto-discovers .python_packages/lib/site-packages/.
        for fs_path in sorted(p for p in site_packages.rglob("*") if p.is_file()):
            arc = fs_path.relative_to(build_dir).as_posix()
            _add_file(zf, arc, fs_path.read_bytes())
    size_mb = zip_path.stat().st_size / (1024 * 1024)
    _step(f"  zip built: {zip_path.name} ({size_mb:.1f} MB, {len(target.files)} entries)")


_ACA_REQUIREMENTS_SRC = "pipeline/Code/requirements-pipeline.txt"
_ACA_STAGE_REQUIREMENTS_NAME = "requirements.txt"


def _build_azure_automation_runbook(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage the runbook source for `az automation runbook replace-content`.

    Azure Automation runbooks are a single Python file. target.files[]
    MUST contain exactly one entry, the runbook source. We copy it into
    build_dir as `runbook.py` (a stable filename the deploy handler can
    find without re-parsing the manifest's source_location). The
    manifest_snapshot writer downstream emits the secret bindings into
    `manifest.json`; the deploy handler reads those and pushes each
    binding into the Automation Account as an Automation Variable
    before pushing the runbook content.
    """
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    if len(target.files) != 1:
        sys.exit(
            f"ERROR: azure_automation_runbook target {target.target_id!r} "
            f"MUST declare exactly one source file (the runbook .py); got "
            f"{len(target.files)} files."
        )
    src_rel = target.files[0].source_location
    src_path = repo_root / src_rel
    if not src_path.is_file():
        sys.exit(
            f"ERROR: runbook source {src_rel!r} not present on disk for "
            f"target {target.target_id!r}."
        )
    if not src_rel.endswith(".py"):
        sys.exit(
            f"ERROR: azure_automation_runbook source MUST be a Python file; "
            f"got {src_rel!r} for {target.target_id!r}."
        )
    dst = build_dir / "runbook.py"
    shutil.copyfile(src_path, dst)
    size_kb = dst.stat().st_size / 1024.0
    _step(f"  staged runbook -> {dst.name} ({size_kb:.1f} KB)")


def _build_azure_container_app(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage the Pipeline source tree + render the Dockerfile.

    layout under build_dir:
        app/pipeline/Code/...          (every target.files[] entry)
        app/pipeline/Code/requirements.txt   (renamed from requirements-pipeline.txt)
        Dockerfile                     (rendered by aca_helpers)

    The Dockerfile's COPY pulls from app/pipeline/Code/ so the Functions
    runtime sees function_app.py + host.json at /home/site/wwwroot/.
    """
    if build_dir.exists():
        shutil.rmtree(build_dir, ignore_errors=True)
    build_dir.mkdir(parents=True)
    app_root = build_dir / "app"
    _step(f"  staging {len(target.files)} JSON-declared files into {app_root}")
    for f in target.files:
        src_path = repo_root / f.source_location
        if not src_path.is_file():
            sys.exit(
                f"ERROR: file in JSON manifest not present on disk: "
                f"{f.source_location}"
            )
        if f.source_location == _ACA_REQUIREMENTS_SRC:
            dst_rel = (Path("pipeline") / "Code" / _ACA_STAGE_REQUIREMENTS_NAME).as_posix()
        else:
            dst_rel = f.source_location
        dst_path = app_root / dst_rel
        dst_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst_path)
    # requirements.txt is required by the Dockerfile's pip install step.
    req_dst = app_root / "pipeline" / "Code" / _ACA_STAGE_REQUIREMENTS_NAME
    if not req_dst.is_file():
        sys.exit(
            f"ERROR: ACA build requires {_ACA_REQUIREMENTS_SRC} in target.files[]; "
            f"absent at {req_dst}"
        )
    (build_dir / "Dockerfile").write_text(
        aca_helpers.aca_render_dockerfile(), encoding="utf-8",
    )
    _step(f"  Dockerfile rendered -> {build_dir / 'Dockerfile'}")


def _augment_manifest_for_aca(
    build_dir: Path,
    target: TargetRecord,
    build_n: int,
) -> None:
    """Append ACA-specific fields to the manifest the snapshot writer
    produced. The deploy handler reads these to know image repo, tag,
    and integrity hash.
    """
    manifest_path = build_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    # Compute image_repo per env from azure_container_app.registry +
    # container_app. We carry per-env image refs so the deploy handler
    # doesn't have to re-derive.
    image_refs: dict[str, dict[str, str]] = {}
    for e in target.environments:
        aca = e.azure_container_app
        if aca is None:
            continue
        registry = aca.get("registry") or aca["resource_group"].lower().replace("-", "")
        repo = f"{registry}.azurecr.io/{aca['container_app']}"
        image_refs[e.env_binding] = {
            "image_repo": repo,
            "image_tag": str(build_n),
            "image_ref": f"{repo}:{build_n}",
        }
    manifest["image_refs"] = image_refs
    manifest["content_hash"] = aca_helpers.aca_content_hash_tree(build_dir / "app")
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _build_one(repo_root: Path, target: TargetRecord, build_n: int, build_sha: str, build_root_rel: Path | None = None) -> Path:
    build_dir = _target_build_dir(repo_root, build_n, target.target_id, build_root_rel)
    _step(f"=== {target.target_kind} {target.target_id} -> {build_dir} ===")
    if target.target_kind == "cloudflare_pages_project":
        _build_cloudflare(repo_root, target, build_dir)
    elif target.target_kind == "hf_space":
        _build_hf_space(repo_root, target, build_dir)
    elif target.target_kind == "azure_function_app":
        _build_azure_function_app(repo_root, target, build_dir)
    elif target.target_kind == "azure_container_app":
        _build_azure_container_app(repo_root, target, build_dir)
    elif target.target_kind == "azure_automation_runbook":
        _build_azure_automation_runbook(repo_root, target, build_dir)
    else:
        raise RuntimeError(
            f"target_kind {target.target_kind!r} not supported in local_build."
        )
    _write_manifest_snapshot(build_dir, target, build_n, build_sha)
    if target.target_kind == "azure_container_app":
        _augment_manifest_for_aca(build_dir, target, build_n)
    return build_dir


_BUILDABLE_KINDS = (
    "cloudflare_pages_project",
    "hf_space",
    "azure_function_app",
    "azure_container_app",
    "azure_automation_runbook",
)


def _select_targets(coll: DeploymentCollection, target_arg: str) -> list[TargetRecord]:
    if target_arg == "all":
        return [t for t in coll if t.target_kind in _BUILDABLE_KINDS]
    if target_arg in ("cloudflare", "cloudflare_pages_project"):
        return [t for t in coll if t.target_kind == "cloudflare_pages_project"]
    if target_arg in ("hf", "hf_space"):
        return [t for t in coll if t.target_kind == "hf_space"]
    if target_arg == "azure":
        return [
            t for t in coll
            if t.target_kind in ("azure_function_app", "azure_container_app")
        ]
    if target_arg == "azure_function_app":
        return [t for t in coll if t.target_kind == "azure_function_app"]
    if target_arg in ("aca", "azure_container_app"):
        return [t for t in coll if t.target_kind == "azure_container_app"]
    if target_arg in ("automation", "azure_automation_runbook"):
        return [t for t in coll if t.target_kind == "azure_automation_runbook"]
    for t in coll:
        if t.target_id == target_arg:
            return [t]
    return []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build per-target deploy packages under localBuild/<target_id>/."
    )
    parser.add_argument(
        "--target", default="all",
        help="'all' | 'cloudflare' | 'hf' | 'azure' | 'aca' | a specific target_id.",
    )
    args = parser.parse_args(argv)

    _require_local_context()
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

    _step(f"built {len(built)} package(s) (build={build_n}):")
    for b in built:
        _step(f"  {b.relative_to(repo_root)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
