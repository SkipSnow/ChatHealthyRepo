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
see DeploymentArchitectureDesignAndMigrationPlanPhase_v14.docx section 2.1 build_chathealthy.py) and is stamped into manifest.json. No secret VALUES are
written into any file in the package; only names+stores appear in
manifest.json. local_deploy reads from this tree.

usage:
    python local_build.py --target all
    python local_build.py --target cloudflare
    python local_build.py --target target_cloudflare_pages_website

Per EPIC-008-F-012.
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


# Canonical build output root (operator directive 2026-08-04):
# `<repo>/build/` is the single well-known location for every build's
# per-target output. build_chathealthy.py purges it entirely at the
# start of every invocation; deploy_chathealthy.py reads only from it.
_BUILD_ROOT_REL = Path("build")
REMOTE_BUILD_ROOT_REL = Path("architecture/DevOpsBuildDeployAndEnvironmentManagement/remoteBuild")



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "FrontEndApplicationLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_frontend_lib.exceptions import ChatHealthyException
    return ChatHealthyException


def _step(msg: str) -> None:
    print(f"[local_build] {msg}", flush=True)


def _find_repo_root(start: Path) -> Path:
    p = start.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"no .git found walking up from {start}")


def _resolve_build_sha(repo_root: Path) -> str:
    """Return the short HEAD SHA. Local builds MUST work without a
    commit, so this never rejects on uncommitted changes."""
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()


from version_counter import (  # noqa: E402
    VERSIONS_DB,
    VERSIONS_COLLECTION,
    versions_collection as _versions_collection,
    read_build_number as _read_dev_build_number,
)


def _target_build_dir(repo_root: Path, build_n: int, target_id: str, build_root_rel: Path | None = None) -> Path:
    root = build_root_rel if build_root_rel is not None else _BUILD_ROOT_REL
    return repo_root / root / target_id


def _declared_packages(target: TargetRecord) -> list[str]:
    """Package ids this target declares, across every env binding.

    A package is a logical set of capabilities and is therefore a property
    of the target, not of the environment it happens to be deployed to.
    The schema nests packages[] under environments[]; reading the union
    across bindings recovers the target-level truth.
    """
    seen: list[str] = []
    for eb in target.environments:
        for pkg in (eb.packages or []):
            pid = pkg.get("package_id")
            if pid and pid not in seen:
                seen.append(pid)
    for f in target.files:
        pid = getattr(f, "package", None)
        if pid and pid not in seen:
            seen.append(pid)
    return seen


def _package_build_dir(target_build_dir: Path, package_id: str) -> Path:
    return target_build_dir / package_id


def _files_by_package(target: TargetRecord) -> dict[str, list[dict]]:
    """Every declared file grouped by the package that owns it.

    The manifest states this two ways and both are authoritative: a file
    may sit in the target's files[] carrying a `package` tag, or nested
    inside environments[].packages[].files[]. Reading only one of them is
    why ten runbook packages looked empty.
    """
    out: dict[str, list[dict]] = {}
    for f in target.files:
        pid = getattr(f, "package", None)
        if pid:
            out.setdefault(pid, []).append(f.to_dict())
    for eb in target.environments:
        for pkg in (eb.packages or []):
            pid = pkg.get("package_id")
            if not pid:
                continue
            bucket = out.setdefault(pid, [])
            known = {d["source_location"] for d in bucket}
            for d in (pkg.get("files") or []):
                if d.get("source_location") not in known:
                    bucket.append(d)
    return out


def _stage_packages(repo_root: Path, target: TargetRecord,
                    target_dir: Path, selection: set[str] | None = None) -> None:
    """Stage each declared package into its own directory.

    Inside a package directory a file sits at its repo-relative
    source_location, so a path in the build tree reads straight back to
    the manifest entry that put it there, and back again.

    A file declared but absent from disk is a hard stop: the manifest
    promised bytes the build cannot produce.
    """
    grouped = {k: v for k, v in _files_by_package(target).items()
               if selection is None or k in selection}
    staged = 0
    missing: list[str] = []
    for pid, entries in grouped.items():
        pkg_dir = _package_build_dir(target_dir, pid)
        for d in entries:
            if d.get("disposition") == "managed":
                continue  # written from the manifest by the materializer
            rel = d["source_location"]
            src = repo_root / rel
            if not src.is_file():
                missing.append(rel)
                continue
            dst = pkg_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            staged += 1
    if missing:
        raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"{target.target_id}: {len(missing)} declared file(s) "
                    f"absent from disk: {sorted(missing)[:5]}")
    _step(f"  staged {staged} declared file(s) across "
          f"{len(grouped)} package(s)")


def materialize_build_structure(manifest_path: Path, build_root: Path) -> tuple[int, int]:
    """Create the full declared build tree: every target, every package.

    The build tree is meant to be a readable map of the deployment
    architecture, so every target the manifest declares has a directory
    and every package inside it has one too -- whether or not this
    invocation produces content for it. An empty package directory is a
    true statement: that capability is declared and not currently built.

    Content is never touched here; only structure is added. Directories
    for targets or packages the manifest no longer declares are removed,
    so the tree cannot drift from the manifest.
    """
    doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    declared: dict[str, list[str]] = {}
    for rec in doc.get("DeploymentTargetRecord", []):
        tid = rec.get("target_id")
        if not tid:
            continue
        pkgs: list[str] = []
        for eb in rec.get("environments", []) or []:
            for pkg in (eb.get("packages") or []):
                pid = pkg.get("package_id")
                if pid and pid not in pkgs:
                    pkgs.append(pid)
        for f in rec.get("files", []) or []:
            pid = f.get("package")
            if pid and pid not in pkgs:
                pkgs.append(pid)
        declared[tid] = pkgs

    build_root.mkdir(parents=True, exist_ok=True)
    for tid, pkgs in declared.items():
        tdir = build_root / tid
        tdir.mkdir(parents=True, exist_ok=True)
        for pid in pkgs:
            (tdir / pid).mkdir(parents=True, exist_ok=True)

    # Prune anything the manifest no longer declares.
    for child in build_root.iterdir():
        if not child.is_dir():
            continue
        if child.name not in declared:
            shutil.rmtree(child, ignore_errors=True)
            _step(f"  pruned undeclared target dir {child.name}")
            continue
        keep = set(declared[child.name]) | {"manifest.json", "_publish"}
        for sub in child.iterdir():
            if sub.name in keep:
                continue
            if sub.is_dir():
                shutil.rmtree(sub, ignore_errors=True)
            else:
                sub.unlink()
    return len(declared), sum(len(v) for v in declared.values())


def _prepare_build_tree(target: TargetRecord, build_dir: Path,
                        selection: set[str] | None = None) -> list[str]:
    """Empty every package directory, leaving the directory structure.

    Each build deletes all content; the shape stays. An empty package
    directory on disk is a visible statement that the manifest declares a
    capability the build produced nothing for -- which is a finding, not
    something to hide by removing the directory.
    """
    declared = _declared_packages(target)
    packages = list(declared)
    if selection is not None:
        chosen = [p for p in packages if p in selection]
        if packages and not chosen:
            raise _ch_exc()(
            mode="value_error",
            component="_build_chain",
            message=f"target {target.target_id!r} declares "
                    f"{packages} but none was selected; name the package(s) "
                    "you intend to build")
        packages = chosen
    if not packages and target.files:
        raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"target {target.target_id!r} has {len(target.files)} "
                    "declared file(s) but no package; every file must name "
                    "the capability it serves")
    # A target with no packages and no files is provisioned, not built --
    # a resource group, a vnet, a managed identity. Its build output is the
    # manifest alone, and inventing an empty package to fill the tree would
    # be ceremony.
    build_dir.mkdir(parents=True, exist_ok=True)
    # The structure is every package the target declares, whether or not
    # this build produces it. Only the SELECTED packages are emptied --
    # emptying the rest would make each targeted build destroy the output
    # of the last one, so deploying a package you did not just rebuild
    # would ship an empty directory.
    for pid in declared:
        pkg_dir = _package_build_dir(build_dir, pid)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        if pid not in packages:
            continue
        for child in pkg_dir.iterdir():
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink()
    # Anything at the target root that is no longer a declared package or
    # the target's own manifest is residue from a previous structure.
    for child in build_dir.iterdir():
        if child.name in declared or child.name == "manifest.json":
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=True)
        else:
            child.unlink()
    return packages


ARCHITECTURE_REL = Path("brain/machine_artifacts/content/deployment_architecture.json")


def _architecture_path(anywhere_under_repo: Path) -> Path:
    """Locate deployment_architecture.json from any path inside the repo."""
    for d in [anywhere_under_repo, *anywhere_under_repo.parents]:
        cand = d / ARCHITECTURE_REL
        if cand.is_file():
            return cand
    raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"deployment_architecture.json not found above "
                    f"{anywhere_under_repo}")


def architecture_digest(path: Path) -> str:
    """sha256 of the manifest, CRLF-normalised so Windows and Linux agree."""
    import hashlib
    raw = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(raw).hexdigest()


def _architecture_digest(path: Path) -> str:
    return architecture_digest(path)


def _write_manifest_snapshot(
    build_dir: Path,
    target: TargetRecord,
    build_n: int,
    build_sha: str,
    packages: list[str] | None = None,
) -> None:
    """Write the target's slice of deployment_architecture.json into the
    build dir, augmented with build_number + build_sha. Embedded file
    bytes are stripped from `files[]` (they're materialized to disk in
    the same build_dir; carrying them in the manifest would double them).
    """
    # Deployment content lives in deployment_architecture.json and nowhere
    # else. This file used to be a copy of the target's slice of it --
    # 24KB for the automation account -- which is a second copy of the
    # truth that can drift from the first. What the build legitimately
    # knows and the manifest cannot is which build this is, so that is all
    # this records, plus the digest of the manifest it was built from. The
    # deploy re-reads the manifest and refuses if the digest moved.
    arch = _architecture_path(build_dir)
    stamp: dict = {
        "build_number": build_n,
        "build_sha": build_sha,
        "target_id": target.target_id,
        "architecture_sha256": _architecture_digest(arch),
    }
    for pid in packages:
        pkg_stamp = dict(stamp)
        pkg_stamp["package_id"] = pid
        (_package_build_dir(build_dir, pid) / "build.json").write_text(
            json.dumps(pkg_stamp, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    if not packages:
        (build_dir / "build.json").write_text(
            json.dumps(stamp, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    stale = build_dir / "manifest.json"
    if stale.is_file():
        stale.unlink()


def _apply_dependency_pins(repo_root: Path, build_dir: Path) -> None:
    """Pin Python dependency versions in any requirements.txt staged into
    build_dir, using the single-source-of-truth `firm.dependency_pins`
    block in deployment_architecture.json. This keeps requirements.txt in
    the source tree readable with `>=` floors while the deployed container
    installs the exact pinned version, so an HF rebuild can never silently
    pick up a new pydantic_ai (or other pinned dep) and break startup.

    Operator's framing: the pin must come from deployment_architecture.json
    and flow through to the per-target build. This function is the
    'flow through' step.
    """
    brain_path = (repo_root / "brain" / "machine_artifacts" / "content"
                  / "deployment_architecture.json")
    if not brain_path.is_file():
        return
    raw = json.loads(brain_path.read_text(encoding="utf-8"))
    pins = (raw.get("firm") or {}).get("dependency_pins") or {}
    if not pins:
        return
    # Map JSON key (underscored) → pip package name (dashed).
    pkg_map = {key: key.replace("_", "-") for key in pins}
    for req_path in build_dir.rglob("requirements.txt"):
        try:
            text = req_path.read_text(encoding="utf-8")
        except Exception:
            continue
        lines = text.splitlines()
        changed = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            for json_key, pkg in pkg_map.items():
                if stripped.startswith(pkg):
                    extras = ""
                    rest = stripped[len(pkg):]
                    if rest.startswith("["):
                        close = rest.find("]")
                        if close >= 0:
                            extras = rest[:close + 1]
                    new_line = f"{pkg}{extras}=={pins[json_key]}"
                    if line != new_line:
                        _step(f"  pin    {req_path.name}: {stripped} -> {new_line}")
                        lines[i] = new_line
                        changed = True
                    break
        if changed:
            req_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


_HF_URL_PLACEHOLDERS = {
    "__HF_URL_SHAREDSERVICES__": "target_hf_space_shared_services",
}


def _compute_hf_space_url_for_build(repo_root: Path, hf_target_id: str, env: str, build_n: int) -> str:
    """Stable-base-name HF Space URL. Reverted 2026-06-22: per-build
    suffix `_<build_n>` is retired — re-deploys go to the same Space.
    `build_n` is accepted for caller compatibility but unused.
    HF publishes Spaces at https://{org}-{name_lower_hyphenated}.hf.space."""
    if env == "local":
        local_map = {
            "target_hf_space_findcare_backend":     "https://localhost:7860",
            "target_hf_space_evaluatecare_backend": "https://localhost:8001",
            "target_hf_space_shared_services":      "https://localhost:8002",
        }
        return local_map[hf_target_id]
    qualified = rd._hf_space_qualified(hf_target_id, env)
    org, base = qualified.split("/", 1)
    return f"https://{org.lower()}-{base.replace('_', '-').lower()}.hf.space"


def _substitute_hf_urls_in_index_html(repo_root: Path, build_dir: Path, env: str,
                                      build_n: int, declared_indexes: int) -> None:
    """Substitute __HF_URL_*__ placeholders in every index.html under
    build_dir with the per-build HF Space URLs computed for this env/build_n.

    Per the no-fallbacks rule: every index.html under the Cloudflare Pages
    build_dir MUST contain ALL placeholders. Missing a placeholder fails
    loud — catches the accident of someone committing substituted output
    back into source.
    """
    targets_for_placeholder = {
        ph: _compute_hf_space_url_for_build(repo_root, tid, env, build_n)
        for ph, tid in _HF_URL_PLACEHOLDERS.items()
    }
    indexes = list(build_dir.rglob("index.html"))
    if len(indexes) != declared_indexes:
        raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"_substitute_hf_urls_in_index_html: the selected package(s) "
            f"declare {declared_indexes} index.html file(s) and {len(indexes)} "
            f"were staged under {build_dir}. The build must produce exactly what "
            f"the manifest declares.")
    if not indexes:
        # The selected package declares no page, so there is nothing to
        # substitute. This is not a shortfall: a schemas-only build ships
        # JSON and no HTML, and the count check above already proved that
        # is what the manifest says.
        return
    for idx in indexes:
        text = idx.read_text(encoding="utf-8")
        # A page that carries NONE of the __HF_URL_*__ placeholders is a
        # static page not participating in HF-URL substitution (e.g. a
        # maintenance page). Skip the require-all check for those. Only
        # fail-loud when a page has SOME placeholders but is missing at
        # least one -- that's the "someone committed substituted output
        # back into source" case.
        present = [p for p in _HF_URL_PLACEHOLDERS if p in text]
        if present and len(present) < len(_HF_URL_PLACEHOLDERS):
            missing = sorted(set(_HF_URL_PLACEHOLDERS) - set(present))
            raise _ch_exc()(
                mode="runtime_error",
                component="_build_chain",
                message=f"_substitute_hf_urls_in_index_html: {idx.relative_to(build_dir)} "
                f"is participating in HF-URL substitution (found {sorted(present)}) "
                f"but is missing {missing!r}. Every participating index.html MUST "
                f"contain every __HF_URL_*__ placeholder verbatim. If a "
                f"substituted URL was committed back into source, restore the "
                f"placeholder."
            )
        for placeholder, url in targets_for_placeholder.items():
            if placeholder in text:
                text = text.replace(placeholder, url)
                _step(f"  hf-url {idx.name}: {placeholder} -> {url}")
        idx.write_text(text, encoding="utf-8")
        final = idx.read_text(encoding="utf-8")
        for placeholder in _HF_URL_PLACEHOLDERS:
            if placeholder in final:
                raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"_substitute_hf_urls_in_index_html: {idx.relative_to(build_dir)} "
                    f"still contains {placeholder!r} after substitution; build aborted.")
        # Post-substitution URL-presence check applies only if the page
        # participated in substitution.
        if present:
            for url in targets_for_placeholder.values():
                if url not in final:
                    raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"_substitute_hf_urls_in_index_html: {idx.relative_to(build_dir)} "
                        f"missing expected URL {url!r} after substitution; build aborted.")


_PACKAGE_ROUTING_KINDS = (
    "host_os_process",
    "cloudflare_pages_project",
    "azure_automation_account",
    "azure_container_registry",
    "azure_key_vault",
    "entra_directory",
)


def _build_cloudflare(repo_root: Path, target: TargetRecord, build_dir: Path,
                     env: str, build_n: int,
                     selection: set[str] | None = None) -> None:
    """Stage each declared package into its own directory under build_dir.

    build_dir here is the TARGET directory, not a package directory: this
    target declares several packages and each file names the one it belongs
    to. Inside a package directory a file sits at its repo-relative
    source_location, so the build tree reads back to the manifest without a
    lookup table.

    Files present under Website/ but declared by no package are NOT staged.
    That is the point of driving from the manifest: an undeclared file is
    one nobody claimed, and shipping it anyway is how the build acquired
    bytes no runtime path uses.
    """
    _stage_packages(repo_root, target, build_dir, selection)
    snippet = ch_fonts_inliner.read_snippet()
    inlined = 0
    for html in build_dir.rglob("*.html"):
        if ch_fonts_inliner.inline_into(html, snippet):
            inlined += 1
    _step(f"  CH_FONTS inlined in {inlined} pages")
    declared_indexes = sum(
        1
        for pid, entries in _files_by_package(target).items()
        if selection is None or pid in selection
        for d in entries
        if d["source_location"].replace("\\", "/").endswith("/index.html")
        or d["source_location"] == "index.html"
    )
    _substitute_hf_urls_in_index_html(
        repo_root, build_dir, env, build_n, declared_indexes
    )
    # Managed bytes are written by _materialize_managed_files after this
    # returns, and that one is package-aware. Materializing here as well
    # wrote the managed Dockerfile at the target root, creating a directory
    # named after its source path that no package declares.


def _build_hf_space(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage the HF Space's source set into build_dir, build React frontend
    (FindCare only), materialize managed bytes, copy the Dockerfile to root.

    build_dir contains the docker build context that local_deploy passes
    to `docker build`. Per Skip's framing: build = source bytes for that
    target; deploy does the install procedure (docker build/push, HF API,
    git push to Space repo).
    """
    build_dir.mkdir(parents=True, exist_ok=True)

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
        raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"unknown hf_space target {target_id!r}")

    for src_rel, dst_rel in source_set:
        _step(f"  stage  {src_rel} -> {dst_rel}")
        rd._copy_tree(repo_root, build_dir, src_rel, dst_rel or src_rel)

    _apply_dependency_pins(repo_root, build_dir)

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
    build_dir.mkdir(parents=True, exist_ok=True)
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


# Modules of chathealthy_frontend_lib we inline into AA runbook files so
# the sandbox can `from chathealthy_frontend_lib.X import Y` without a
# separately-installed package. Every byte in the assembled preamble is
# derived from the corresponding source file committed to git; the
# assembly happens deterministically at build time.
_INLINE_LIB_MODULES = (
    "exceptions",       # no in-package deps
    "logging_service",  # imports .exceptions eagerly; imports .mongo_utilities lazily inside _MongoLogHandler.emit
    "mongo_utilities",  # imports .exceptions AND .logging_service eagerly at module load; logging_service MUST be installed first
    "pipeline_boot",    # AA runbook Mongo-logging bootstrap: KV Mongo secret fetch + SRV->direct URI + CHLS env setup. Stdlib-only + optional certifi. Peer module; installed after logging_service/mongo_utilities so callers can compose.
)


def _inline_chathealthy_lib_if_used(repo_root: Path, runbook_path: Path) -> None:
    """If the staged runbook has any `from chathealthy_frontend_lib...`
    import, prepend a self-contained bootstrap block that installs the
    library's modules into sys.modules at runtime. Source bytes for each
    inlined module are read at build time from
    FrontEndApplicationLib/src/chathealthy_frontend_lib/<mod>.py — the
    same git-committed source the Docker image installs at pip time.
    No external hosting, no wheel; the runbook file shipped to Azure
    Automation contains every byte required for the imports to resolve."""
    body = runbook_path.read_text(encoding="utf-8")
    if "chathealthy_frontend_lib" not in body:
        return

    import base64 as _b64_mod
    lib_src_root = repo_root / "FrontEndApplicationLib" / "src" / "chathealthy_frontend_lib"
    installs: list[str] = []
    for mod in _INLINE_LIB_MODULES:
        mod_path = lib_src_root / f"{mod}.py"
        if not mod_path.is_file():
            sys.exit(
                f"ERROR: cannot inline chathealthy_frontend_lib for runbook "
                f"{runbook_path.name}: source file {mod_path} is missing."
            )
        src_bytes = mod_path.read_bytes()
        encoded = _b64_mod.b64encode(src_bytes).decode("ascii")
        installs.append(f'_install("{mod}", "{encoded}")')

    preamble_lines = [
        "# --- BEGIN inlined chathealthy_frontend_lib (built from git commit at build time) ---",
        "# Bytes below are base64-encoded copies of",
        "# FrontEndApplicationLib/src/chathealthy_frontend_lib/{exceptions,mongo_utilities,logging_service,pipeline_boot}.py",
        "# assembled by _build_chain.py:_inline_chathealthy_lib_if_used.",
        "import base64 as _b64, sys as _sys, types as _types",
        "_pkg = _types.ModuleType('chathealthy_frontend_lib')",
        "_pkg.__path__ = []",
        "_sys.modules['chathealthy_frontend_lib'] = _pkg",
        "",
        "def _install(_name, _src_b64):",
        "    _mod = _types.ModuleType('chathealthy_frontend_lib.' + _name)",
        "    _mod.__package__ = 'chathealthy_frontend_lib'",
        "    _sys.modules['chathealthy_frontend_lib.' + _name] = _mod",
        "    _src = _b64.b64decode(_src_b64).decode('utf-8')",
        "    exec(compile(_src, '<inlined:chathealthy_frontend_lib.' + _name + '>', 'exec'), _mod.__dict__)",
        "    setattr(_pkg, _name, _mod)",
        "",
        *installs,
        "",
        "del _install, _pkg, _b64, _sys, _types",
        "# --- END inlined chathealthy_frontend_lib ---",
        "",
    ]
    preamble = "\n".join(preamble_lines)

    # Preamble must come AFTER any `from __future__` line (Python
    # requires __future__ imports first) and AFTER the module docstring
    # if present.
    fut_m = _re_mod.search(r"^from __future__ import [^\n]+\n", body, _re_mod.MULTILINE)
    if fut_m:
        pos = fut_m.end()
    else:
        doc_m = _re_mod.match(r'\s*("""(?:[^"\\]|\\.|"(?!""))*""")\s*\n', body)
        pos = doc_m.end() if doc_m else 0
    new_body = body[:pos] + preamble + body[pos:]
    runbook_path.write_text(new_body, encoding="utf-8")
    new_kb = runbook_path.stat().st_size / 1024.0
    _step(f"  inlined chathealthy_frontend_lib -> {runbook_path.name} ({new_kb:.1f} KB)")


def _runbook_packages(target: TargetRecord) -> list[tuple[str, dict]]:
    """(package_id, package) for every runbook package on this target."""
    out: list[tuple[str, dict]] = []
    seen: set[str] = set()
    for eb in target.environments:
        for pkg in (eb.packages or []):
            pid = pkg.get("package_id")
            if pkg.get("kind") == "runbook" and pid and pid not in seen:
                seen.add(pid)
                out.append((pid, pkg))
    return out


def _build_automation_account(repo_root: Path, target: TargetRecord,
                              target_dir: Path,
                              selection: set[str] | None = None) -> None:
    """Build each runbook package of the Automation Account.

    One destination, many capabilities. Each runbook package produces
    `<package_id>/runbook.py` -- the stable filename the deploy handler
    looks for -- with the shared library inlined, because an Automation
    runbook is a single file with no import path.
    """
    packages = [(pid, pkg) for pid, pkg in _runbook_packages(target)
                if selection is None or pid in selection]
    if not packages:
        _step("  no runbook packages declared")
        return
    for pid, pkg in packages:
        files = pkg.get("files") or []
        if len(files) != 1:
            sys.exit(
                f"ERROR: runbook package {pid!r} on {target.target_id!r} "
                f"MUST declare exactly one source file; got {len(files)}."
            )
        src_rel = files[0]["source_location"]
        if not src_rel.endswith(".py"):
            sys.exit(
                f"ERROR: runbook package {pid!r} source MUST be a Python "
                f"file; got {src_rel!r}."
            )
        src_path = repo_root / src_rel
        if not src_path.is_file():
            sys.exit(
                f"ERROR: runbook source {src_rel!r} not present on disk "
                f"for package {pid!r}."
            )
        pkg_dir = _package_build_dir(target_dir, pid)
        pkg_dir.mkdir(parents=True, exist_ok=True)
        dst = pkg_dir / "runbook.py"
        shutil.copyfile(src_path, dst)
        _step(f"  {pid}: staged runbook.py ({dst.stat().st_size / 1024.0:.1f} KB)")
        _inline_chathealthy_lib_if_used(repo_root, dst)
        if pid == "change_db_version":
            _emit_change_db_version_target_url_registry(repo_root, pkg_dir)
        if pid in ("ca_bootstrap", "ca_endpoint"):
            _inline_ca_helpers(repo_root, pkg_dir)


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
    build_dir.mkdir(parents=True, exist_ok=True)
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

    _inline_chathealthy_lib_if_used(repo_root, dst)

    if target.target_id == "target_azure_automation_runbook_change_db_version":
        _emit_change_db_version_target_url_registry(repo_root, build_dir)
    if target.target_id in (
        "target_azure_automation_runbook_ca_bootstrap",
        "target_azure_automation_runbook_ca_endpoint",
    ):
        _inline_ca_helpers(repo_root, build_dir)


def _inline_ca_helpers(repo_root: Path, build_dir: Path) -> None:
    """Inline pipeline/deploy/ca_helpers.py into the staged CA runbook.

    Azure Automation runbooks are a single Python file (see
    _build_azure_automation_runbook). ca_helpers.py stays a shared source
    module on disk for local use and for the chathealthy_ca client mirror;
    at build time we embed it into runbook.py the same way change_db_version
    gets its URL registry inlined — so the AA-deployed artifact is one file
    with no sibling imports.
    """
    helpers_path = repo_root / "pipeline" / "deploy" / "ca_helpers.py"
    if not helpers_path.is_file():
        sys.exit(
            f"ERROR: cannot inline ca_helpers — missing {helpers_path}"
        )
    helpers_src = helpers_path.read_text(encoding="utf-8")
    runbook_py = build_dir / "runbook.py"
    original = runbook_py.read_text(encoding="utf-8")
    # Authors keep `import ca_helpers as ch` (plus optional sys.path insert)
    # in source; build replaces that import with an in-process module.
    import_line = "import ca_helpers as ch"
    if import_line not in original:
        sys.exit(
            f"ERROR: cannot inline ca_helpers — {import_line!r} not found in "
            f"{runbook_py}. CA runbook sources must keep that import literal."
        )
    # Strip the optional sibling-path insert that only matters on disk.
    cleaned = original
    path_insert = (
        "sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n\n"
    )
    cleaned = cleaned.replace(path_insert, "", 1)
    # Drop the short comment block that documents the path insert, if present.
    for comment in (
        "# Ensure sibling ca_helpers.py resolves whether the runbook is run from\n"
        "# the deploy dir or dropped into the Automation Account root.\n",
    ):
        cleaned = cleaned.replace(comment, "")
    inline_block = (
        "# --- inlined from pipeline/deploy/ca_helpers.py at build time ---\n"
        "import types as _ch_types\n"
        f"_ca_helpers_src = {helpers_src!r}\n"
        '_ca_helpers_mod = _ch_types.ModuleType("ca_helpers")\n'
        "exec(_ca_helpers_src, _ca_helpers_mod.__dict__)\n"
        "ch = _ca_helpers_mod\n"
        "# --- end inlined ca_helpers ---\n"
    )
    staged = cleaned.replace(import_line, inline_block, 1)
    runbook_py.write_text(staged, encoding="utf-8")
    size_kb = runbook_py.stat().st_size / 1024.0
    _step(f"  inlined ca_helpers into {runbook_py.name} ({size_kb:.1f} KB)")


def _emit_change_db_version_target_url_registry(repo_root: Path, build_dir: Path) -> None:
    """Bake change_db_version_target_url_registry.json sibling to the runbook.

    The runbook reads ChatHealthyConfig.DBVersions, walks each env doc's targets[],
    and POSTs /admin/swap on each target. Target URLs come from this
    registry — NOT from deployment_architecture.json at runtime. The
    registry is a {env: {target_id: node_address}} map derived here at
    build time from the manifest's environments[].node_address for every
    hf_space target. Per EPIC-010-F-101-S-005-REQ-B-004.
    """
    manifest_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    registry: dict[str, dict[str, str]] = {}
    for rec in data.get("DeploymentTargetRecord", []):
        if rec.get("target_kind") != "hf_space":
            continue
        tid = rec.get("target_id")
        for env_entry in rec.get("environments", []):
            env = env_entry.get("env_binding")
            hf = env_entry.get("huggingface_space") or {}
            space = hf.get("space")
            if not (env and tid and space):
                continue
            # The /admin/swap endpoint lives on the HF Space's serving
            # host, NOT on the wrapper path. node_address for hf_space
            # targets points at the user-facing wrapper URL ('dev.chat
            # healthy.ai/findcare') which is a static route, not an API
            # proxy — POSTing /admin/swap there returns 405. Derive the
            # HF Space serving URL from huggingface_space.space using
            # HF's documented convention: lowercase 'owner-name.hf.space'
            # with underscores becoming hyphens.
            owner, _, name = space.partition("/")
            host = f"{owner.lower()}-{name.lower().replace('_', '-')}.hf.space"
            registry.setdefault(env, {})[tid] = f"https://{host}"
    out = build_dir / "change_db_version_target_url_registry.json"
    out.write_text(json.dumps(registry, indent=2) + "\n", encoding="utf-8")
    target_count = sum(len(v) for v in registry.values())
    _step(f"  baked URL registry -> {out.name} ({len(registry)} envs, {target_count} entries)")

    # Inline the registry into the staged runbook.py so the AA-deployed
    # runbook (which only carries the .py, no sibling files) can resolve
    # target URLs at runtime. Replaces a single-line placeholder in the
    # source; the placeholder string MUST match the literal in
    # pipeline/Code/change_db_version.py exactly.
    runbook_py = build_dir / "runbook.py"
    if runbook_py.is_file():
        original = runbook_py.read_text(encoding="utf-8")
        placeholder = "_BAKED_REGISTRY: dict = {}"
        replacement = f"_BAKED_REGISTRY: dict = {json.dumps(registry, separators=(', ', ': '))}"
        if placeholder not in original:
            sys.exit(
                f"ERROR: cannot inline registry into runbook — placeholder "
                f"{placeholder!r} not found in {runbook_py}. The source file "
                "pipeline/Code/change_db_version.py must keep the "
                "placeholder literal verbatim on a single line."
            )
        runbook_py.write_text(original.replace(placeholder, replacement, 1), encoding="utf-8")
        _step(f"  inlined registry into {runbook_py.name}")


def _build_azure_container_app(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage the Pipeline source tree + render the Dockerfile.

    layout under build_dir:
        app/pipeline/Code/...          (every target.files[] entry)
        app/pipeline/Code/requirements.txt   (renamed from requirements-pipeline.txt)
        Dockerfile                     (rendered by aca_helpers)

    The Dockerfile's COPY pulls from app/pipeline/Code/ so the Functions
    runtime sees function_app.py + host.json at /home/site/wwwroot/.
    """
    build_dir.mkdir(parents=True, exist_ok=True)
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


def _build_host_os_process(repo_root: Path, target: TargetRecord, build_dir: Path) -> None:
    """Stage a target that runs as a process on the host OS.

    The layout beneath build_dir mirrors the repository, because for this
    target kind the layout IS the contract. The enforcement manager derives
    its root from its own location (parents[3]), so a manager staged at
    architecture/EngineeringRuleEnforcement/code/ resolves every worker
    beneath it with no path rewriting anywhere. Flatten the tree and that
    property is gone.

    A dotnet_project in the file list is published rather than copied. The
    service executes build/publish/ClaudeCodeConversationPersistenceService.exe; `dotnet build`
    writes bin/, which is why building repeatedly once changed nothing the
    service ran.
    """
    build_dir.mkdir(parents=True, exist_ok=True)

    projects = [f for f in target.files if f.handler_type == "dotnet_project"]
    if len(projects) > 1:
        sys.exit(
            f"ERROR: host_os_process target {target.target_id!r} declares "
            f"{len(projects)} dotnet_project files; at most one is supported."
        )

    # build_dir is the package directory when this target declares one
    # package, and the target directory when it declares several. In the
    # multi-package case each file lands under the package that owns it,
    # and the repository layout is mirrored inside that package -- the
    # enforcement manager resolves its workers relative to its own
    # location, so the relative shape has to survive the split.
    multi = len(_declared_packages(target)) > 1

    def _root_for(f) -> Path:
        if not multi:
            return build_dir
        if not getattr(f, "package", None):
            sys.exit(
                f"ERROR: {target.target_id!r} routes by package but "
                f"{f.source_location!r} names none."
            )
        return _package_build_dir(build_dir, f.package)

    staged = 0
    for f in target.files:
        if f.handler_type == "dotnet_project":
            continue
        src = repo_root / f.source_location
        if not src.is_file():
            sys.exit(
                f"ERROR: missing source file {f.source_location!r} for "
                f"target {target.target_id!r}."
            )
        dst = _root_for(f) / f.source_location
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        staged += 1
    _step(f"  staged {staged} file(s), repository layout mirrored")

    if not projects:
        return

    proj_rel = projects[0].source_location
    proj_src = repo_root / proj_rel
    if not proj_src.is_file():
        sys.exit(f"ERROR: missing dotnet project {proj_rel!r}.")
    publish_dir = (_root_for(projects[0]) / Path(proj_rel).parent
                   / "build" / "publish")
    _step(f"  dotnet publish {Path(proj_rel).name} -> {publish_dir.relative_to(build_dir)}")
    result = subprocess.run(
        ["dotnet", "publish", str(proj_src), "-c", "Release",
         "-o", str(publish_dir), "--nologo", "-v", "quiet"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        sys.exit(
            f"ERROR: dotnet publish failed for {proj_rel!r} "
            f"(exit {result.returncode}):\n{result.stdout}\n{result.stderr}"
        )
    exe = publish_dir / (Path(proj_rel).stem + ".exe")
    if not exe.is_file():
        sys.exit(
            f"ERROR: dotnet publish produced no {exe.name} in {publish_dir}. "
            f"The service cannot start from a publish output that has no "
            f"executable."
        )
    _step(f"  published {exe.name} ({exe.stat().st_size / 1024.0:.1f} KB)")



def _materialize_managed_files(target: TargetRecord, build_dir: Path,
                               target_dir: Path | None = None,
                               selection: set[str] | None = None) -> None:
    """Write every managed file's bytes from the manifest into the build.

    A managed file's content belongs to deployment_architecture.json -- a
    Dockerfile is rendered from its layout array, not read from a copy in
    the source tree. Keeping a second copy on disk coupled the business
    tree to the build tree, which is the thing the manifest exists to
    decouple, and let the two drift with the disk copy being the one people
    edit and the manifest copy being the one that ships.

    `target_dir` is passed when the target routes files by package; each
    managed file is then written under its own package rather than into a
    single flat context.
    """
    for f in target.files:
        if f.disposition != "managed":
            continue
        if selection is not None and getattr(f, "package", None) not in selection:
            continue
        content = f.embedded_content
        if content is None:
            sys.exit(
                f"ERROR: managed file {f.source_location!r} on target "
                f"{target.target_id!r} has no embedded_content; the manifest "
                f"owns these bytes and there is nothing to write."
            )
        if target_dir is not None:
            if not f.package:
                sys.exit(
                    f"ERROR: managed file {f.source_location!r} on target "
                    f"{target.target_id!r} names no package."
                )
            context_root = _package_build_dir(target_dir, f.package)
        else:
            context_root = build_dir
        dst = context_root / f.source_location
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_text(content, encoding="utf-8")
        # Docker needs it at the context root too.
        if dst.name == "Dockerfile" and dst.parent != context_root:
            (context_root / "Dockerfile").write_text(content, encoding="utf-8")
        _step(f"  materialised managed {f.source_location}")


def _build_one(repo_root: Path, target: TargetRecord, build_n: int, build_sha: str, build_root_rel: Path | None = None, env: str = "local", packages_wanted: set[str] | None = None) -> Path:
    target_dir = _target_build_dir(repo_root, build_n, target.target_id, build_root_rel)
    packages = _prepare_build_tree(target, target_dir, packages_wanted)
    selected = set(packages)
    _step(f"=== {target.target_kind} {target.target_id} -> {target_dir} "
          f"[{', '.join(packages)}] ===")
    # Staging writes into the package directory. A single-package target
    # stages wholly into it. A multi-package target must route each file by
    # its own package tag -- picking the first declared package would put
    # bytes under a capability name that does not describe them, which is
    # the untraceability this structure exists to remove.
    # Routing keys off what the target DECLARES, not what this build
    # selected. Deciding on the selection meant building one package of a
    # multi-package target took the single-package path and nested the
    # files a level too deep.
    declared_all = _declared_packages(target)
    if not declared_all:
        build_dir = target_dir
    elif len(declared_all) == 1:
        build_dir = _package_build_dir(target_dir, declared_all[0])
    elif target.target_kind in _PACKAGE_ROUTING_KINDS:
        build_dir = target_dir
    else:
        raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"target {target.target_id!r} declares {len(declared_all)} "
                    f"packages ({', '.join(declared_all)}) but target_kind "
                    f"{target.target_kind!r} has no package-routing stager; "
                    "staging would have to guess which package owns which "
                    "bytes")
    if target.target_kind == "cloudflare_pages_project":
        _build_cloudflare(repo_root, target, build_dir, env, build_n, selected)
    elif target.target_kind == "hf_space":
        _build_hf_space(repo_root, target, build_dir)
    elif target.target_kind == "azure_function_app":
        _build_azure_function_app(repo_root, target, build_dir)
    elif target.target_kind == "azure_container_app":
        _build_azure_container_app(repo_root, target, build_dir)
    elif target.target_kind == "azure_automation_runbook":
        _build_azure_automation_runbook(repo_root, target, build_dir)
    elif target.target_kind == "host_os_process":
        _build_host_os_process(repo_root, target, build_dir)
    elif target.target_kind == "azure_automation_account":
        _build_automation_account(repo_root, target, target_dir, selected)
    elif target.target_kind in (
        "atlas",
        "azure_resource_group",
        "azure_key_vault",
        "azure_storage_account",
        "azure_vnet",
        "identity",
        "entra_directory",
        "azure_container_registry",
        "azure_container_apps_environment",
        "azure_container_app_job",
    ):
        # F-012: shell / identity / ACR / ACA Env / Job definition packages
        # are manifests only (deploy owns Azure create; jobs build images
        # at deploy via az acr build from dockerfile path in the target).
        # _prepare_build_tree has already emptied the package directory;
        # removing it here would delete the declared structure.
        _stage_packages(repo_root, target, target_dir, selected)
    else:
        raise _ch_exc()(
            mode="runtime_error",
            component="_build_chain",
            message=f"target_kind {target.target_kind!r} not supported in local_build.")
    routes = (target.target_kind in _PACKAGE_ROUTING_KINDS
              and len(declared_all) > 1)
    _materialize_managed_files(target, build_dir,
                               target_dir if routes else None, selected)
    # The manifest describes the TARGET, so it sits at the target root
    # above the package directories, not inside one of them.
    _write_manifest_snapshot(target_dir, target, build_n, build_sha,
                            packages)
    if target.target_kind == "azure_container_app":
        _augment_manifest_for_aca(target_dir, target, build_n)
    # Return the TARGET directory, not the package directory. manifest.json
    # lives at the target root, and the caller stamps env/sha onto it; when
    # this returned the package directory the stamp was written next to a
    # manifest that was not there, leaving env unset on every
    # single-package target.
    return target_dir


_BUILDABLE_KINDS = (
    "host_os_process",
    "cloudflare_pages_project",
    "hf_space",
    "azure_function_app",
    "azure_container_app",
    "azure_automation_runbook",
    "atlas",
    "azure_resource_group",
    "azure_key_vault",
    "azure_storage_account",
    "azure_vnet",
    "identity",
    "entra_directory",
    "azure_container_registry",
    "azure_container_apps_environment",
    "azure_container_app_job",
    "azure_automation_account",
    "docker_local_container",
)

_PIPELINE_BUILD_KINDS = (
    "atlas",
    "azure_resource_group",
    "azure_key_vault",
    "azure_storage_account",
    "azure_vnet",
    "identity",
    "entra_directory",
    "azure_container_registry",
    "azure_container_apps_environment",
    "azure_container_app_job",
    "azure_automation_account",
    "azure_automation_runbook",
)


def _select_targets(coll: DeploymentCollection, target_arg: str) -> list[TargetRecord]:
    # No 'all'. Every build names its destinations. A comma-separated list
    # of target_ids is the normal form; the kind selectors below remain for
    # genuinely kind-wide work.
    if "," in target_arg:
        wanted = [p.strip() for p in target_arg.split(",") if p.strip()]
        by_id = {t.target_id: t for t in coll}
        unknown = [w for w in wanted if w not in by_id]
        if unknown:
            raise _ch_exc()(
            mode="value_error",
            component="_build_chain",
            message=f"unknown target_id(s): {unknown}")
        return [by_id[w] for w in wanted]
    if target_arg == "pipeline":
        return [t for t in coll if t.target_kind in _PIPELINE_BUILD_KINDS]
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
    if target_arg in ("host", "host_os_process"):
        return [t for t in coll if t.target_kind == "host_os_process"]
    for t in coll:
        if t.target_id == target_arg:
            return [t]
    return []


# Helper-only module — no main() entry point. Per build_deploy_promote_plan
# v3 §INV-5 the only entry points are build_chathealthy.py + deploy_chathealthy.py
# + promote_chathealthy.py; this module is imported by them, not invoked
# directly.
