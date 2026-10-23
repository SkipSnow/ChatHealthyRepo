# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""pipeline_image_build -- BUILD-time manufacture of the pipeline docker images.

Operator rulings 2026-10-22 (scope: the pipeline epic, EPIC-010):

  - A synthetic artifact derived from other artifacts is manufactured by the
    BUILD, never the deploy. The two derived index JSONs
    (pipeline_indexes.json, pipeline_index_packages.json) are produced here
    from deployment_architecture.json, written durably into the build output
    under build/<target>/<package>/, and materialized into the docker context
    for the image build.

  - Every package/image is 100% COMPLETE at build time. The docker images are
    built HERE -- CA material fetched and baked, derived JSONs included --
    tagged <repo>:build-<n> and <repo>:content-<hash> in the local docker
    daemon, and described by an image_manifest.json in the build output. The
    deploy pushes an already-built image and manufactures nothing.

Imported-only helper (Rule-065 stmt 8): no entry-point block.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
from pathlib import Path

from chathealthy_lib.exceptions import ChatHealthyException

IMAGE_MANIFEST_NAME = "image_manifest.json"

# The derived index artifacts every pipeline image bakes; the record
# (deployment_architecture.json, target_atlas_pipeline) is the one source.
DERIVED_INDEX_FILES = ("pipeline_indexes.json", "pipeline_index_packages.json")


def derive_index_artifacts(repo_root: Path) -> dict[str, str]:
    """The two container-portable index JSONs, derived from the record.

    pipeline_indexes.json: the pipeline_indexes package config (the pipeline
    cluster's slice of the index catalog -- a front-end index never ships to a
    pipeline container). pipeline_index_packages.json: every mongo_indexes
    package on target_atlas_pipeline carrying version_source, keyed by
    package_id -- pipeline_index_catalog.py reads this inside the container,
    which never carries the manifest. Returns {filename: json_text}.
    """
    content_dir = repo_root / "brain" / "machine_artifacts" / "content"
    arch = json.loads(
        (content_dir / "deployment_architecture.json").read_text(encoding="utf-8"))
    pipe_cfg: dict = {}
    pkg_catalog: dict = {}
    for rec in arch.get("DeploymentTargetRecord", []):
        if rec.get("target_id") != "target_atlas_pipeline":
            continue
        for e in rec.get("environments", []):
            for p in (e.get("packages") or []):
                if p.get("package_id") == "pipeline_indexes":
                    pipe_cfg = p.get("config") or {}
                if (p.get("kind") == "mongo_indexes"
                        and (p.get("config") or {}).get("version_source")):
                    pkg_catalog[p["package_id"]] = p["config"]
    if not pkg_catalog:
        raise ChatHealthyException(
            mode="config_error", component="pipeline_image_build",
            message="deployment_architecture.json carries no versioned "
                    "mongo_indexes packages on target_atlas_pipeline; the "
                    "pipeline images cannot be built complete")
    return {
        "pipeline_indexes.json": json.dumps(pipe_cfg, indent=2),
        "pipeline_index_packages.json": json.dumps(pkg_catalog, indent=2),
    }


def _ca_build_args() -> dict[str, str]:
    """The CA public PEMs as docker build args, fetched from Key Vault at
    BUILD time (F-003: CA baked into the image; no runtime KV for CA)."""
    from pipeline_azure_deploy import _az_secret  # noqa: PLC0415
    kv_name = "kv-chpipeline-dev"
    ca_root_pem = _az_secret(kv_name, "ca-root-cert", default="")
    ca_int_pem = _az_secret(kv_name, "ca-intermediate-cert", default="")
    if not ca_root_pem or not ca_int_pem:
        raise ChatHealthyException(
            mode="aborted", component="pipeline_image_build",
            message=f"KV {kv_name!r} missing ca-root-cert or "
                    f"ca-intermediate-cert secrets (F-003 prerequisite)")
    return {
        "BUILDKIT_INLINE_CACHE": "1",
        "CHATHEALTHY_CA_ROOT_B64":
            base64.b64encode(ca_root_pem.encode("utf-8")).decode("ascii"),
        "CHATHEALTHY_CA_INTERMEDIATE_B64":
            base64.b64encode(ca_int_pem.encode("utf-8")).decode("ascii"),
    }


def _docker_image_id(ref: str) -> str:
    p = subprocess.run(["docker", "images", "-q", ref],
                       capture_output=True, text=True, shell=False)
    return (p.stdout or "").strip()


def build_images_for_target(repo_root: Path, target, target_dir: Path,
                            packages: list[dict], build_n: int,
                            log=print, all_packages: list[dict] | None = None) -> int:
    """Build every docker_image package of this target, completely, locally.

    Managed files and the derived index JSONs are materialized into the
    source tree (the docker context) for the build and removed afterwards --
    the durable copies live in build/<target>/<package>/. Each image is
    tagged <repo>:build-<n> and <repo>:content-<hash>; an
    image_manifest.json in the package's build directory tells the deploy
    exactly what to push. Returns the number of images built.
    """
    from pipeline_azure_deploy import run_streamed  # noqa: PLC0415

    docker_pkgs = [p for p in packages if p.get("kind") == "docker_image"]
    if not docker_pkgs:
        return 0

    content_dir = repo_root / "brain" / "machine_artifacts" / "content"
    derived = derive_index_artifacts(repo_root)
    ca_args = _ca_build_args()

    wrote_paths: list[Path] = []
    built = 0
    try:
        # Managed files: the record owns the bytes; materialize for the build.
        # Drawn from EVERY package on the target, not just the selected ones:
        # a managed file one package declares (requirements-pipeline.txt on
        # pipeline_vm_image) is COPY'd by every image's Dockerfile, so a
        # medicare-only build selection must still materialize it.
        for pkg in (all_packages or docker_pkgs):
            for f in pkg.get("files", []) or []:
                if f.get("disposition") == "managed" and f.get("source_location"):
                    out = repo_root / f["source_location"]
                    out.parent.mkdir(parents=True, exist_ok=True)
                    content = f.get("embedded_content") or ""
                    if content.startswith("__base64__:"):
                        out.write_bytes(base64.b64decode(
                            content[len("__base64__:"):], validate=True))
                    else:
                        out.write_bytes(content.encode("utf-8"))
                    wrote_paths.append(out)
        # Derived artifacts: durable in build output, transient in the context.
        for fname, text in derived.items():
            ctx_out = content_dir / fname
            ctx_out.write_text(text, encoding="utf-8")
            wrote_paths.append(ctx_out)
        log(f"  materialized {len(wrote_paths)} file(s) for the image builds "
            f"({', '.join(DERIVED_INDEX_FILES)} derived from the record)")

        for pkg in docker_pkgs:
            pkg_id = pkg.get("package_id")
            config = pkg.get("config", {}) or {}
            repo = config.get("image_repository") or pkg_id
            dockerfile = config.get("dockerfile")
            build_context = config.get("build_context", ".")
            if not dockerfile:
                raise ChatHealthyException(
                    mode="aborted", component="pipeline_image_build",
                    message=f"docker_image package {pkg_id!r} declares no dockerfile")
            pkg_dir = target_dir / str(pkg_id)
            pkg_dir.mkdir(parents=True, exist_ok=True)
            for fname, text in derived.items():
                (pkg_dir / fname).write_text(text, encoding="utf-8")

            content_hash = _dockerfile_context_hash(dockerfile, build_context)
            build_tag = f"build-{build_n}"
            content_tag = f"content-{content_hash[:12]}"
            refs = [f"{repo}:{build_tag}", f"{repo}:{content_tag}"]
            cmd = ["docker", "build",
                   "--file", str((repo_root / dockerfile).resolve())]
            for k, v in ca_args.items():
                cmd += ["--build-arg", f"{k}={v}"]
            for ref in refs:
                cmd += ["--tag", ref]
            cmd.append(str((repo_root / build_context).resolve()))
            import os as _os  # noqa: PLC0415
            env = _os.environ.copy()
            env["DOCKER_BUILDKIT"] = "1"
            env["BUILDKIT_PROGRESS"] = "plain"
            log(f"  docker build {refs[0]}")
            rc, out, log_path = run_streamed(
                cmd, env=env, label="docker build",
                log_name=f"docker_build_{repo}_{build_tag}")
            if rc != 0:
                raise ChatHealthyException(
                    mode="aborted", component="pipeline_image_build",
                    message=f"docker build failed (exit {rc}) for {refs[0]}\n"
                            f"  full build log: {log_path}\n"
                            f"  output tail:\n{out.strip()[-3000:]}")
            image_id = _docker_image_id(refs[0])
            if not image_id:
                raise ChatHealthyException(
                    mode="aborted", component="pipeline_image_build",
                    message=f"docker build reported success but {refs[0]} is "
                            f"not in the local daemon")
            (pkg_dir / IMAGE_MANIFEST_NAME).write_text(json.dumps({
                "package_id": pkg_id,
                "image_repository": repo,
                "build_tag": build_tag,
                "content_tag": content_tag,
                "content_hash": content_hash,
                "image_id": image_id,
                "build": build_n,
                "dockerfile": dockerfile,
            }, indent=2), encoding="utf-8")
            log(f"  built {refs[0]} (image_id {image_id}); manifest -> "
                f"{pkg_dir / IMAGE_MANIFEST_NAME}")
            built += 1
    finally:
        for out in wrote_paths:
            try:
                if out.is_file():
                    out.unlink()
            except OSError:
                pass
        if wrote_paths:
            log(f"  cleaned {len(wrote_paths)} transient context file(s)")
    return built


def _dockerfile_context_hash(dockerfile_path: str, build_context: str) -> str:
    """Compute a stable sha256 covering the Dockerfile itself + every file
    under each `COPY <src>` path referenced by the Dockerfile.

    Only source paths that get baked into the image affect the hash —
    ambient files under the build_context that aren't COPY'd don't count.

    Excludes __pycache__ / .git / .pytest_cache to keep the hash stable
    across dev-machine noise."""
    df = Path(dockerfile_path).resolve()
    ctx = Path(build_context).resolve()
    h = hashlib.sha256()
    h.update(df.read_bytes())
    # Parse COPY <src> [<src2> ...] <dst> — take every token except the
    # last as a source path relative to build_context.
    copy_srcs: list[str] = []
    for line in df.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        copy_srcs.extend(parts[1:-1])
    _EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules", ".venv"}
    for src in copy_srcs:
        root = (ctx / src).resolve()
        if not root.exists():
            # COPY of a non-existent path would fail the build too; still
            # hash the missing-path token so the hash captures the request.
            h.update(f"MISSING:{src}\n".encode("utf-8"))
            continue
        if root.is_file():
            h.update(f"{root.relative_to(ctx)}\n".encode("utf-8"))
            h.update(root.read_bytes())
            continue
        for f in sorted(root.rglob("*")):
            if any(part in _EXCLUDE_DIRS for part in f.parts):
                continue
            if not f.is_file():
                continue
            h.update(f"{f.relative_to(ctx)}\n".encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()
