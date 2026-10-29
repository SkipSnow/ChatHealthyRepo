"""build_chathealthy.py — the unified build entry point.

Replaces the legacy local_build.py / remote_build.py pair. One script,
one CLI surface, internally dispatches by GITHUB_ACTIONS context.

CLI:
  python build_chathealthy.py --env {local|dev|qa|prod} [--target <list>]

Behaviour by --env:
  local        — sources the working tree as-is (no branch check, no commit
                 required, no admin.Versions counter bump). Stamps the
                 current admin.Versions latest.build onto manifest.json.
  dev|qa|prod  — enforces the env-branch guard (dev->dev branch, qa->qa,
                 prod->main), then BUMPS admin.Versions.build by one and
                 stamps the new value onto manifest.json.

manifest.json fields (per build_deploy_promote_plan v3 §C.1/§C.4):
  env             — the env this build is for; deploy_chathealthy.py rejects
                    a mismatch (gate check b).
  git_head_sha    — short git HEAD SHA at build time (gate check a).
  build           — the build counter at build time (gate check c).
  built_at        — ISO-8601 UTC; diagnostic and audit only.

Reference: build_deploy_promote_plan v3 §C.1 (build responsibilities),
§INV-1 (local from working tree), §INV-2 (non-local from env's branch).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Per-target_kind handlers, helper utilities, crosswalk gate — all
# imported from local_build.py for now. They migrate in step 5.7.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from devops_identity import establish_azure_identity  # noqa: E402
from _build_chain import (
    _declared_packages,  # noqa: E402
    package_install_order,
    materialize_build_structure,
    _build_one,
    _find_repo_root,
    _read_dev_build_number,
    _versions_collection,
    VERSIONS_DB,
    VERSIONS_COLLECTION,
    _resolve_build_sha,
    _select_targets,
    _step,
    AgileBacklogLoader,
    Crosswalk,
    DeploymentCollection,
    RecordLoader,
    SecretsResolver,
)

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
_CH_LOG = ChatHealthyLoggingService()


VALID_ENVS = ("local", "dev", "qa", "prod")
_ENV_BRANCH = {"local": "dev", "dev": "dev", "qa": "qa", "prod": "main"}


def _materialize_env_file(env: str, canonical_repo: Path, build_dir: Path) -> Path:
    """Write the .env this build/deploy will use to <build_dir>/.env,
    then return that path.

      * --env local          -> copy the working-tree file <repo>/.env
                                (operator's own file; the operator
                                maintains this).
      * --env dev|qa|prod    -> copy the same working-tree file. It holds
                                this workstation's operator credentials,
                                which is what a build runs on. The secrets
                                the deployed application runs on are NOT
                                here: they are read one at a time from the
                                environment's own Key Vault at deploy time.

    Downstream build steps (leak-check, mongo connect, etc.) and the
    matching deploy MUST read <build_dir>/.env — never the working-tree
    .env for cloud envs, never anywhere else. Every build re-materialises
    the file from scratch; no stale copies from prior builds survive."""
    dst = build_dir / ".env"
    if env == "local":
        src = canonical_repo / ".env"
        if not src.is_file():
            raise ChatHealthyException(
                mode="aborted",
                component="build_chathealthy",
                message=f"ERROR: --env local requires {src}; not found.")
        shutil.copy2(src, dst)
        _step(f"materialised {dst} from working-tree .env")
        # Same as the cloud branch below: the materialised file is the .env
        # this build uses, so it belongs in the environment. Without this,
        # --env local copied the file and then ran with none of it loaded,
        # and anything reading KEY_VAULT_URI failed on a build whose .env
        # was sitting right there.
        from dotenv import load_dotenv
        load_dotenv(dst, override=False)
        return dst
    # A cloud build used to fetch a secret literally named `env-file` from
    # kv-chpipeline-dev: one Key Vault secret holding an entire dotenv
    # document, 116 names, and it was the source for dev, qa and prod alike
    # because the vault name was written into this call and carried no
    # environment. Three things were wrong with it at once.
    #
    # It handed every environment every credential. One read produced the
    # whole set -- every API key, the identity secrets, the values that reach
    # the certificate authority -- so a build for one environment held what
    # it needed to act as any other. That is the zero-trust model inverted:
    # the blob sat in the same vault as ca-root-privatekey, and reading it
    # required nothing beyond the access needed to build.
    #
    # It made rotation unmanageable. A key lives once in a vault and is
    # rotated by one write; inside a blob it is a substring of a compressed
    # document that has to be fetched, decoded, edited and re-uploaded whole.
    #
    # And it was a copy nobody reconciled. The blob was maintained by hand
    # against a .env also maintained by hand, with no comparison between them
    # ever performed, so which of the two was current on any given day was
    # unknowable.
    #
    # The blob is not read any more. Secrets a target runs on are read one at
    # a time from the environment's own vault, at deploy, through
    # SecretsResolver's azure_key_vault store. What is materialised here is
    # only what this build itself runs on -- the workstation's own operator
    # credentials, used to reach Atlas for the build number and to sign az
    # calls. Those are this machine acting as itself, not the deployed
    # application's secrets, and every name the blob carried is present here.
    src = canonical_repo / ".env"
    if not src.is_file():
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=f"ERROR: a {env} build needs the operator credentials in "
                    f"{src} to reach Atlas and Azure; not found.")
    shutil.copy2(src, dst)
    _step(f"materialised {dst} from the workstation .env (build-time "
          f"credentials only; deployed secrets come from the {env} vault)")
    from dotenv import load_dotenv
    load_dotenv(dst, override=False)
    return dst


def _current_branch(repo_root: Path) -> str:
    return subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()


def _get_build_source(canonical_repo: Path, env: str) -> Path:
    """Return the directory the build reads its sources from.

      --env local            -> the working tree (build reads whatever the
                                operator has on disk).
      --env dev | qa | prod  -> a fresh checkout of origin/<branch> in a
                                temp directory. The local working tree,
                                current branch, and any uncommitted files
                                are irrelevant to and untouched by the
                                build. Caller MUST pair this with
                                _release_build_source() when done."""
    if env == "local":
        return _materialise_working_tree(canonical_repo)
    branch = _ENV_BRANCH[env]
    # A cloud build is already running out of a fresh checkout of
    # origin/<branch>: chain_provenance.reexec_from_branch fetched the branch,
    # added a worktree at its tip and re-executed this program from it, and
    # the CHATHEALTHY_CHAIN_SOURCE marker carries that tip's sha. That tree is
    # the build source. Fetching and checking the same commit out a second
    # time produced nothing the first checkout did not already hold -- it
    # only doubled the checkout and left a second temp tree to remove.
    chain_checkout = _chain_checkout_root()
    if chain_checkout is not None:
        _step(f"build source: the chain's own origin/{branch} checkout at "
              f"{chain_checkout} (no second checkout)")
        return chain_checkout
    _run_git(["fetch", "origin", branch], canonical_repo, "fetch")
    import tempfile
    src = Path(tempfile.mkdtemp(prefix=f"build_{env}_{branch}_"))
    _CH_LOG.info(f"[build] materialising origin/{branch} at {src}")
    _run_git(
        ["worktree", "add", "--detach", "--force", str(src), f"origin/{branch}"],
        canonical_repo, "worktree add",
    )
    return src


def _chain_checkout_root() -> Path | None:
    """The temp worktree this process was re-executed from, or None when this
    process is the parent (or a local build) and runs out of the repository
    itself. The checkout is identified structurally: a git worktree carries
    .git as a file, the canonical repository as a directory."""
    import chain_provenance as _cp  # noqa: PLC0415
    if not _cp.running_from_git():
        return None
    here = Path(__file__).resolve()
    for candidate in (here, *here.parents):
        marker = candidate / ".git"
        if marker.exists():
            return candidate if marker.is_file() else None
    return None


def _materialise_working_tree(canonical_repo: Path) -> Path:
    """A copy of the working tree, for local to build in.

    A build writes: vite writes its bundle, npm writes node_modules, the
    chain writes managed files. Doing that where the source lives left
    build output in the source tree -- a React bundle under frontend/dist
    and a second copy of it under backend/static, both of them ignored by
    git so nothing ever objected, and neither of them what actually ships.

    local is not an exception to how a build works. Its SOURCE is the
    working tree rather than a branch -- that is the whole point of local,
    and uncommitted work must be built -- but the BUILD happens in a
    materialised copy exactly as it does for dev, qa and prod.

    What is copied is what git would carry: every tracked file, plus every
    untracked file that is not ignored. So node_modules, dist, build
    output and caches do not come along, and the copy starts as clean as a
    fresh worktree does. npm ci then installs into the copy, which is what
    every other environment already does.
    """
    import tempfile
    src = Path(tempfile.mkdtemp(prefix="build_local_"))
    listed = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=str(canonical_repo), capture_output=True, text=True, check=True,
    ).stdout
    names = [n for n in listed.split("\0") if n]
    _CH_LOG.info(f"[build] materialising the working tree at {src} "
                 f"({len(names)} file(s))")
    for name in names:
        source = canonical_repo / name
        if not source.is_file():
            # git lists a submodule or a file deleted since it was staged.
            continue
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return src


def _release_build_source(build_source: Path, canonical_repo: Path) -> None:
    """Remove whatever _get_build_source materialised.

    dev, qa and prod get a git worktree, which git owns and git removes.
    local gets a plain copy, which git knows nothing about, so it is
    removed as a directory.
    """
    if build_source == canonical_repo:
        return
    if build_source == _chain_checkout_root():
        # The chain's checkout belongs to the parent process that re-executed
        # us from it; it removes the tree when this child exits.
        return
    if (build_source / ".git").exists():
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(build_source)],
            cwd=str(canonical_repo), capture_output=True, text=True,
        )
        return
    shutil.rmtree(build_source, ignore_errors=True)


def _export_built_packages(built: list[Path], build_source: Path, canonical_repo: Path) -> list[Path]:
    """Copy built package dirs out of the temp build source back to the
    canonical repo's localBuild/ so deploy_chathealthy.py finds them at
    the well-known location. No-op for --env local."""
    if build_source == canonical_repo:
        return built
    exported: list[Path] = []
    for pkg in built:
        rel = pkg.relative_to(build_source)
        dst = canonical_repo / rel
        if dst.exists():
            shutil.rmtree(dst, ignore_errors=True)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(pkg, dst)
        exported.append(dst)
    return exported


def _run_git(args: list[str], cwd: Path, label: str) -> None:
    r = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=f"ERROR: git {label} failed (rc={r.returncode}). "
            f"stderr: {r.stderr.strip()}")


def _bump_build_counter(env: str) -> int:
    """For --env dev|qa|prod: insert a new admin.Versions latest doc with
    build = prior_build + 1 and git_number = current HEAD SHA. Returns
    the new build number. --env local does not call this."""
    from dotenv import load_dotenv

    repo_root = _find_repo_root(Path(__file__))
    # Canonical env source per operator directive 2026-08-04.
    build_env = repo_root / "build" / ".env"
    working_tree_env = repo_root / ".env"
    # Both, not the first that exists: build/.env is a snapshot from an older
    # build and can be missing keys the working tree has. load_dotenv does not
    # override what is already set, so the earlier file still wins per key.
    # build/.env is a snapshot from an older build and can carry values that
    # have since been corrected -- on 2026-08-08 it still held a dead Azure
    # tenant. The working tree is authoritative, so it loads last and wins.
    for candidate in (build_env, working_tree_env):
        if candidate.is_file():
            load_dotenv(candidate, override=True)
    # override=True also carries the application's logging facts into this
    # process -- CH_LOG_DESTINATION=mongo, CH_LOG_DB=pipelineAdmin. Those
    # belong to the deployed app. A build is workstation tooling and its log
    # is the operator's terminal; inheriting them made the build depend on a
    # Mongo write it holds no grant for.
    os.environ["CH_LOG_DESTINATION"] = "stderr"
    # DevOpsUser, not the application's connection string: bumping the build
    # counter is a devops act. And pipelineAdmin, not admin: Atlas refuses
    # writes to admin through any custom role.
    # The shared library is not pip-installed in the workstation venv; every
    # repo script reaches it by path.
    sys.path.insert(0, str(repo_root / "ChatHealthyLib" / "src"))
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    from cluster_host import host_for as _cluster_host
    coll = (ChatHealthyMongoUtilities()
            .getConnection("DevOpsUser", "ChatHealthyFrontEnd",
                            host=_cluster_host("ChatHealthyFrontEnd"))[VERSIONS_DB][VERSIONS_COLLECTION])
    latest = coll.find_one(sort=[("from", -1)])
    if latest is None:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} has no records.")
    prior = latest.get("build")
    if prior is None:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} latest record "
                 "has no 'build' field.")
    new_build = int(prior) + 1
    git_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root), capture_output=True, text=True, check=True,
    ).stdout.strip()
    new_doc = {
        "build": new_build,
        "git_number": git_sha,
        "version": latest.get("version", ""),
        "from": datetime.now(timezone.utc).isoformat(),
    }
    coll.insert_one(new_doc)
    _step(f"{VERSIONS_DB}.{VERSIONS_COLLECTION} bumped: build {prior} -> {new_build}")
    return new_build


def _run_from_branch_checkout(env: str, argv, script_name: str):
    """Hand a cloud run over to the same program checked out of its branch.

    Returns the child's exit code, or None when this process carries on.
    Separate from main so main stays a driver.
    """
    import chain_provenance as _cp  # noqa: PLC0415
    import sys as _sys  # noqa: PLC0415
    # One build at a time: sweep a prior killed build's residue before this run
    # creates its own. Parent only -- the re-exec child runs inside one of these
    # temp worktrees and must not delete its own ground.
    if not _cp.running_from_git():
        _sweep_build_residue(_find_repo_root(Path(__file__)))
    return _cp.reexec_from_branch(
        env,
        f"architecture/DevOpsBuildDeployAndEnvironmentManagement/{script_name}",
        list(argv if argv is not None else _sys.argv[1:]))


def _prepare_workstation(env: str) -> tuple[Path, Path]:
    """The three workstation facts a build needs before it reads any source.

    The build acts as DevOpsUser rather than as whoever is at the terminal,
    which governs every az subprocess the chain spawns downstream. The output
    root persists and each build empties only the package directories it was
    asked to produce; purging it here made every build a full build in effect.
    And .env is materialised into it, which downstream steps read through
    os.environ.
    """
    establish_azure_identity("DevOpsUser")
    canonical_repo = _find_repo_root(Path(__file__))
    canonical_build_dir = canonical_repo / "build"
    canonical_build_dir.mkdir(parents=True, exist_ok=True)
    _materialize_env_file(env, canonical_repo, canonical_build_dir)
    return canonical_repo, canonical_build_dir


# target -> the package the registry ships inside. It must land in the
# package, not beside it: the package IS the Docker build context, so a
# file one level up is written and never shipped.
TOOL_REGISTRY_TARGETS = {"target_hf_space_shared_services": "service_runtime"}
TOOL_REGISTRY_FILENAME = "tool_registry.py"


# The website carries its build as a file, because there is nothing on a
# static site to ask. It is deployed on its own cadence, so it will lag or
# lead the servers, and the session shows what the browser actually loaded
# rather than assuming one number for everything.
BUILD_IDENTITY_TARGETS = {"target_cloudflare_pages_website": "runtime_driver"}
BUILD_IDENTITY_FILENAME = "build.js"


def _generate_build_identity(target_id: str, package_dir: Path, env: str,
                             build_n: int, build_sha: str) -> None:
    package = BUILD_IDENTITY_TARGETS.get(target_id)
    if package is None:
        return
    destination = package_dir / package / "Website"
    if not destination.is_dir():
        destination = package_dir / package
        if not destination.is_dir():
            return
    payload = {
        "build": build_n, "commit": build_sha, "env": env,
        "built_at": datetime.now(timezone.utc).isoformat(),
    }
    (destination / BUILD_IDENTITY_FILENAME).write_text(
        "window.CH_BUILD = " + json.dumps(payload) + ";" + chr(10),
        encoding="utf-8")
    _step(f"  build identity: {target_id}/{package}/"
          f"{BUILD_IDENTITY_FILENAME} build={build_n}")


def _generate_tool_registry(repo_root: Path, target_id: str,
                            package_dir: Path) -> None:
    """Write the tool registry into a package that reasons about tools.

    Derived from the source this build was made from, so it names the tools
    this build carries and no others. Written after the package is
    populated -- written before, the package build replaces the directory
    and the registry goes with it. Regenerated every build; never in the
    git tree.
    """
    package = TOOL_REGISTRY_TARGETS.get(target_id)
    if package is None:
        return
    destination = package_dir / package
    if not destination.is_dir():
        return
    from tool_registry_generator import write_registry
    count = write_registry(repo_root, destination / TOOL_REGISTRY_FILENAME)
    _step(f"tool registry: {count} tool(s) -> "
          f"{target_id}/{package}/{TOOL_REGISTRY_FILENAME}")


def _delete_build_root(canonical_build_dir: Path) -> None:
    """The whole tree, before anything is written into it.

    Pruning only what the manifest no longer declares is not the same
    thing: a file deleted from the repository leaves its staged copy
    behind, inside a target that is still declared, and nothing removes it.
    That is how architecture/DevOpsBuildDeployAndEnvironmentManagement/
    localBuild/ came to hold two copies of a file that had been deleted
    from the repository on 2026-07-09 -- staged that morning, orphaned that
    afternoon, and still on disk seven weeks later carrying a MongoDB
    connection string. Gitignored, so no gate could ever read it.

    A build directory is derived: everything in it comes from source and
    nothing is authored there. Deleting it costs one rebuild of whatever
    this invocation names and guarantees the property that matters -- what
    is in the tree came from this build, from source that exists.
    """
    if not canonical_build_dir.exists():
        return
    removed = sum(1 for _ in canonical_build_dir.rglob("*") if _.is_file())
    shutil.rmtree(canonical_build_dir, ignore_errors=False)
    _step(f"build root deleted: {removed} stale file(s) removed from "
          f"{canonical_build_dir.name}/")


def _sweep_build_residue(canonical_repo: Path) -> None:
    """Remove every temp worktree, temp build tree and stale lock left by a
    prior build. There is never more than one build at a time, so all of it is
    residue from a run killed before its own finally-cleanup ran -- garbage."""
    import glob  # noqa: PLC0415
    import tempfile  # noqa: PLC0415
    tmp = Path(tempfile.gettempdir())
    prefixes = ("chain_dev_", "chain_qa_", "chain_prod_",
                "build_dev_", "build_qa_", "build_prod_", "build_local_")
    # Registered temp worktrees first, so git forgets them before the dirs go.
    listed = subprocess.run(
        ["git", "worktree", "list", "--porcelain"], cwd=str(canonical_repo),
        capture_output=True, text=True).stdout
    for line in listed.splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree "):].strip())
        if path.name.startswith(prefixes):
            subprocess.run(["git", "worktree", "remove", "--force", str(path)],
                           cwd=str(canonical_repo), capture_output=True, text=True)
            shutil.rmtree(path, ignore_errors=True)
    # Any unregistered temp dirs carrying our prefixes.
    for prefix in prefixes:
        for leftover in glob.glob(str(tmp / (prefix + "*"))):
            shutil.rmtree(leftover, ignore_errors=True)
    # Registrations whose dirs are already gone, and stale index locks.
    subprocess.run(["git", "worktree", "prune"], cwd=str(canonical_repo),
                   capture_output=True, text=True)
    for lock in (canonical_repo / ".git").glob("next-index-*.lock"):
        try:
            lock.unlink()
        except OSError:
            pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build per-target deploy packages under localBuild/<target_id>/."
    )
    parser.add_argument(
        "--env", required=True, choices=VALID_ENVS,
        help="Target environment for the build. Determines branch check + "
             "whether the admin.Versions counter is bumped.",
    )
    parser.add_argument(
        "--target", required=True,
        help="Comma-separated target_id list. There is no 'all': every build "
             "names the destinations it is building. Group selectors "
             "('cloudflare' | 'hf' | 'azure' | 'aca' | 'pipeline' | 'host') "
             "remain for kind-wide work and still require --package.",
    )
    parser.add_argument(
        "--package", default="",
        help="Deprecated and not needed. A build produces every package the "
             "named targets declare, read from deployment_architecture.json. "
             "Supplying a list that disagrees with the record is an illegal "
             "state and stops the build.",
    )
    args = parser.parse_args(argv)

    from_git = _run_from_branch_checkout(args.env, argv, "build_chathealthy.py")
    if from_git is not None:
        return from_git

    canonical_repo, canonical_build_dir = _prepare_workstation(args.env)

    # Pull sources from the correct place per env:
    #   local           -> the working tree
    #   dev | qa | prod -> a temp checkout of origin/<branch>
    repo_root = _get_build_source(canonical_repo, args.env)
    # Everything downstream reads the manifest from the SAME place the code
    # comes from. For a cloud build that is origin/<branch>, never the
    # workstation.
    import hf_helpers as _rd
    _rd.set_build_source(repo_root)
    _step(f"repo_root={repo_root} env={args.env} target={args.target}")

    outcome = "failed(aborted)"
    try:
        _delete_build_root(canonical_build_dir)

        _n_targets, _n_packages = materialize_build_structure(
            repo_root / "brain" / "machine_artifacts" / "content"
            / "deployment_architecture.json",
            canonical_build_dir,
        )
        _step(f"build structure: {_n_targets} target(s), {_n_packages} package(s)")
        rc = _build_body(args, repo_root, canonical_repo, canonical_build_dir)
        outcome = "succeeded" if rc == 0 else f"failed(rc={rc})"
        return rc
    except BaseException as exc:
        outcome = f"failed({type(exc).__name__})"
        raise
    finally:
        _release_build_source(repo_root, canonical_repo)
        _report_timeline(args.env, outcome)


# The build number this process produced, once it is known. The timeline is
# recorded against it at the end of the run.
_BUILD_NUMBER: list[int] = []


def _report_timeline(env: str, outcome: str) -> None:
    """Say where the time went, then keep the record.

    The table goes to the terminal on every build. The record goes onto the
    build's own BuildVersions row for dev, qa and prod -- local bumps no
    counter and so has no row. A failure to write the record is reported and
    does not change the build's outcome: the build already happened.
    """
    import chain_provenance as _cp  # noqa: PLC0415
    tl = _cp.timeline()
    label = f"build {_BUILD_NUMBER[0]}" if _BUILD_NUMBER else "build (no number)"
    for line in tl.summary_lines(f"{label} {outcome}"):
        _CH_LOG.info(f"[local_build] {line}")
    if env == "local" or not _BUILD_NUMBER:
        return
    try:
        from version_counter import record_build_timeline  # noqa: PLC0415
        record_build_timeline(_BUILD_NUMBER[0], tl.to_doc(outcome))
        _CH_LOG.info(f"[local_build] timeline recorded on "
                     f"{VERSIONS_DB}.{VERSIONS_COLLECTION} build={_BUILD_NUMBER[0]}")
    except Exception as exc:  # noqa: BLE001 - telemetry must not change the outcome
        _CH_LOG.warning(f"[local_build] timeline not recorded: "
                        f"{type(exc).__name__}: {exc}")


def _build_body(args, repo_root: Path, canonical_repo: Path, canonical_build_dir: Path) -> int:
    # The sha names the source this build came from. dev, qa and prod build
    # a git worktree whose HEAD is the branch tip. local builds a copy of
    # the working tree, which carries no .git of its own, so the sha it was
    # copied from is the canonical repository's HEAD.
    build_sha = _resolve_build_sha(
        canonical_repo if args.env == "local" else repo_root)
    _step(f"HEAD={build_sha}")

    brain_path = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    backlog_path = repo_root / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
    # .env comes from the canonical build dir (materialized at build
    # start: working tree for --env local, KV for --env dev|qa|prod).
    # NEVER read from a git source (working tree or temp worktree) at
    # this call site — that would sneak the operator's dev tree into
    # cloud builds via the leak-check.
    env_file = canonical_build_dir / ".env"

    # The build does not write deployment_architecture.json. The record is
    # the instruction; a program that edits its own instruction before
    # obeying it is not governed by it. What this build measured is
    # recorded in each package's own build record, which the deploy reads.

    # Scope schema validation to the single target being built when a
    # specific target_id was requested. For kind aliases ('cloudflare',
    # 'hf', 'azure', 'aca') and 'all', validate the whole envelope.
    # A comma-separated list names several targets; the loader filters to
    # one, so validate the whole envelope in that case.
    load_filter = (args.target
                   if args.target.startswith("target_")
                   and "," not in args.target else None)
    coll: DeploymentCollection = RecordLoader().load_collection(
        brain_path, target_id_filter=load_filter,
    )
    # No secret is read, checked or resolved by the build. Resolution is the
    # deploy's, per name, from the store the declaration binds
    # (EPIC-008-F-012-S-004-REQ-B-009); a build directory is not a place a
    # credential belongs.
    targets = _select_targets(coll, args.target)
    if not targets:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=f"ERROR: no targets matched --target={args.target!r}")
    # The record declares which packages a target carries. --package may name
    # a subset of them to build just those; it may not name a package the
    # target does not declare, which would be a second, conflicting
    # declaration of one fact. Dropping --package builds every declared one.
    # A subset build is the operator's explicit choice, not a silent omission,
    # so it does not drift the record the way an unstated omission would.
    known = {p for t in targets for p in _declared_packages(t, args.env)}
    if not known:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=f"ERROR: --target={args.target!r} declares no packages.")

    supplied = {p.strip() for p in (args.package or "").split(",") if p.strip()}
    if supplied:
        undeclared = supplied - known
        if undeclared:
            raise ChatHealthyException(
                mode="illegal_state",
                component="build_chathealthy",
                message=f"ERROR: --package={sorted(supplied)} names package(s) "
                f"{sorted(undeclared)} that --target={args.target!r} does not "
                f"declare: {sorted(known)}. Name only packages the target "
                f"carries, or drop --package to build them all.")
    if args.env != "local" and not supplied and len(known) > 1:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=(f"ERROR: --target={args.target!r} carries {len(known)} "
                     f"packages ({sorted(known)}) but --package was not given; "
                     f"name the package(s) to build. Each package builds on its "
                     f"own -- there is no 'all packages' shortcut, so touching "
                     f"one capability never rebuilds the others. Local builds "
                     f"are exempt."))
    packages_wanted = supplied or set(known)
    for _t in targets:
        _step(f"  {_t.target_id}: install order {package_install_order(_t)}")
    _step(f"building every declared package {sorted(packages_wanted)} "
          f"across {len(targets)} target(s)")

    if args.env == "local":
        build_n = _read_dev_build_number()
        _step(f"build_number={build_n} (read from pipelineAdmin.Versions; --env local does not bump)")
    else:
        build_n = _bump_build_counter(args.env)
        _step(f"build_number={build_n} (counter bumped)")
    _BUILD_NUMBER.append(build_n)

    from version_counter import record_package_build

    built: list[Path] = []
    total_stamped = 0
    for t in targets:
        package_dir = _build_one(repo_root, t, build_n, build_sha,
                                 env=args.env, packages_wanted=packages_wanted)
        # The build has happened; record which packages this build produced.
        # A package not named keeps the build it already held, which is what
        # gives each package a lifecycle of its own on one shared sequence.
        for pid in sorted(packages_wanted & set(_declared_packages(t, args.env))):
            record_package_build(t.target_id, pid, build_n, args.env, build_sha)
            _step(f"  recorded {t.target_id}/{pid} build={build_n}")
        _generate_tool_registry(repo_root, t.target_id, package_dir)
        _generate_build_identity(t.target_id, package_dir, args.env,
                                 build_n, build_sha)
        total_stamped += _stamp_env_on_manifest(package_dir, args.env, build_sha, build_n)
        built.append(package_dir)

    # Whole-build guard. A single target can legitimately build nothing -- an
    # infrastructure target (resource group, key vault, storage, vnet, identity)
    # in a --target group declares no package and is provisioned at deploy, not
    # built -- so the empty check belongs to the build as a whole, not to each
    # target. The build is only wrong if it stamped NO package across every
    # target it touched.
    if total_stamped == 0:
        raise ChatHealthyException(
            mode="aborted",
            component="build_chathealthy",
            message=(f"ERROR: --target={args.target!r} --package="
                     f"{sorted(packages_wanted)} produced no package to stamp "
                     f"across any target."))

    # Export built packages to the canonical <repo>/build/ location so
    # deploy_chathealthy.py finds them regardless of where they were
    # actually assembled (temp worktree for dev|qa|prod; already the
    # canonical location for local).
    if repo_root != canonical_repo:
        # Copy back one PACKAGE at a time, never the target directory. The
        # target directory holds every package the target declares, and the
        # temp tree has content only for the ones this build named -- so
        # replacing the target wholesale deleted the last build of every
        # sibling. A site assembled from all its packages then had only the
        # one just built, and a single-package deploy could never produce a
        # complete tree.
        selected = set(packages_wanted)
        exported: list[Path] = []
        for tgt in built:
            rel = tgt.relative_to(repo_root / "build")
            dst_target = canonical_build_dir / rel
            dst_target.mkdir(parents=True, exist_ok=True)
            for child in sorted(tgt.iterdir()):
                if child.is_dir() and child.name not in selected:
                    continue
                dst = dst_target / child.name
                if dst.exists():
                    shutil.rmtree(dst, ignore_errors=True) if dst.is_dir() else dst.unlink()
                if child.is_dir():
                    shutil.copytree(child, dst)
                else:
                    shutil.copy2(child, dst)
            exported.append(dst_target)
        built = exported

    _step(f"built {len(built)} package(s) (env={args.env}, build={build_n}):")
    for b in built:
        _step(f"  {b.relative_to(canonical_repo)}")
    return 0


def _stamp_env_on_manifest(target_dir: Path, env: str, git_head_sha: str,
                           build_n: int) -> int:
    """Stamp the deploy-gate facts onto every package's build.json and return
    how many packages were stamped.

    These are build facts -- which env this was built for, from which
    commit, as which build -- and they belong to the package, because a
    selective build produces some packages and leaves others at whatever
    they already were.

    This wrote to manifest.json, a per-target copy of the deployment
    manifest that no longer exists. It silently did nothing once that file
    went away, so packages carried no env and the deploy rejected them as
    'built for env None'.

    A target that produced no package -- an infrastructure target that declares
    none -- stamps nothing and returns 0; whether that is an error is the
    caller's whole-build decision, not this per-target function's.
    """
    stamped = 0
    for stamp_path in sorted(target_dir.glob("*/build.json")):
        data = json.loads(stamp_path.read_text(encoding="utf-8"))
        data["env"] = env
        data["git_head_sha"] = git_head_sha
        data["build"] = build_n
        data["built_at"] = datetime.now(timezone.utc).isoformat()
        stamp_path.write_text(
            json.dumps(data, indent=2) + "\n", encoding="utf-8"
        )
        stamped += 1
    return stamped


if __name__ == "__main__":
    sys.exit(main())
