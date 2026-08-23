"""The build counter, in one place.

BuildVersions, not Versions: the collection records builds -- number,
commit, timestamp -- and the release version is the same string on
every row. A name that says "versions" invited the confusion with
DBVersions, which is a different fact entirely (which data collections
an environment binds to).

There were three readers of it -- _build_chain, hf_helpers, _deploy_chain --
and moving it off admin.Versions took three separate discoveries, each one
found by a build failing in a new way. This module exists so the next move
is one edit.

Not admin: Atlas refuses writes to the admin database through any custom
role, deliberately, since admin is the security database. A counter living
there can never be bumped by an identity we are able to define; it only
worked while atlasAdmin accounts existed.

And not pipelineAdmin, which is the PIPELINE's metadata store -- the
pipeline has no use for a build version. frontEndAdmin is the front end's
equivalent, and the version record is a front-end fact. Putting it in
pipelineAdmin left admin.Versions behind as a stale second copy that the
services kept reading; they were seven builds apart.

And DevOpsUser, not the front end's identity. Building is a devops act, and
routing it through the front end's credential is how making a build succeed
turned into widening the front end's grants.
"""
from __future__ import annotations

import sys
from pathlib import Path
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

VERSIONS_DB = "frontEndAdmin"
VERSIONS_COLLECTION = "BuildVersions"


def _repo_root() -> Path:
    p = Path(__file__).resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    from chathealthy_lib.exceptions import ChatHealthyException
    raise ChatHealthyException(
        mode="repo_root_not_found",
        component="VersionCounter",
        message=f"no .git found walking up from {__file__}")


def versions_collection():
    """The Versions collection, as DevOpsUser."""
    lib_src = _repo_root() / "ChatHealthyLib" / "src"
    if str(lib_src) not in sys.path:
        sys.path.insert(0, str(lib_src))
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return (ChatHealthyMongoUtilities()
            .getConnection("DevOpsUser", "ChatHealthyFrontEnd")[VERSIONS_DB][VERSIONS_COLLECTION])


def latest_record() -> dict:
    """The newest Versions document. Callers that need version or framework
    alongside the counter read it once from here rather than opening their
    own connection."""
    latest = versions_collection().find_one(sort=[("from", -1)])
    if latest is None:
        raise ChatHealthyException(
            mode="aborted",
            component="version_counter",
            message=f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} has no records.")
    return latest


def read_build_number() -> int:
    """The current global counter. One int shared across envs per
    build_deploy_promote_plan v3 (§3); per-env slots were removed."""
    latest = versions_collection().find_one(sort=[("from", -1)])
    if latest is None:
        raise ChatHealthyException(
            mode="aborted",
            component="version_counter",
            message=f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} has no records.")
    build = latest.get("build")
    if build is None:
        raise ChatHealthyException(
            mode="aborted",
            component="version_counter",
            message=f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} latest record has "
            f"no 'build' field.")
    return int(build)


PACKAGE_BUILDS_COLLECTION = "PackageBuilds"


def package_builds_collection():
    """Which build each package was last produced by.

    One counter for the system; a package sits wherever it last landed on
    it. So findcare's service_runtime can hold 1970 while the website's
    static_pages still holds 1967 -- different points on one sequence, not
    separate sequences, which is what keeps the numbers comparable across
    packages.

    Append-only, like the other frontEndAdmin records: a row per build of a
    package, and the newest row is what that package currently holds.
    """
    lib_src = _repo_root() / "ChatHealthyLib" / "src"
    if str(lib_src) not in sys.path:
        sys.path.insert(0, str(lib_src))
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return (ChatHealthyMongoUtilities()
            .getConnection("DevOpsUser", "ChatHealthyFrontEnd")
            [VERSIONS_DB][PACKAGE_BUILDS_COLLECTION])


EVENT_BUILT = "built"
EVENT_DEPLOYED = "deployed"


def _record(event: str, target_id: str, package_id: str, build: int,
            env: str, git_sha: str | None = None) -> None:
    from datetime import datetime, timezone
    package_builds_collection().insert_one({
        "event": event,
        "target_id": target_id,
        "package_id": package_id,
        "build": int(build),
        "env": env,
        "git_sha": git_sha,
        "at": datetime.now(timezone.utc),
    })


def record_package_build(target_id: str, package_id: str, build: int,
                         env: str, git_sha: str) -> None:
    """Record that this build produced this package."""
    _record(EVENT_BUILT, target_id, package_id, build, env, git_sha)


def record_package_deploy(target_id: str, package_id: str, build: int,
                          env: str) -> None:
    """Record that this build of this package landed on this target.

    Built and deployed are different facts: a package can be produced and
    never shipped, and the same build can land in dev, then qa, then prod.
    Keeping both events is what lets the question "which build of this
    package is live in prod" be answered without asking prod.
    """
    _record(EVENT_DEPLOYED, target_id, package_id, build, env)


def read_package_build(target_id: str, package_id: str) -> int | None:
    """The build that last produced this package, or None if never built."""
    row = package_builds_collection().find_one(
        {"event": EVENT_BUILT, "target_id": target_id,
         "package_id": package_id},
        sort=[("at", -1)],
    )
    return None if row is None else int(row["build"])


def read_package_deployed(target_id: str, package_id: str,
                          env: str) -> int | None:
    """The build of this package currently live on this target in this env."""
    row = package_builds_collection().find_one(
        {"event": EVENT_DEPLOYED, "target_id": target_id,
         "package_id": package_id, "env": env},
        sort=[("at", -1)],
    )
    return None if row is None else int(row["build"])
