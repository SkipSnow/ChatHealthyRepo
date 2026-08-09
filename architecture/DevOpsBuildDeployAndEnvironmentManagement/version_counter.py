"""The build counter, in one place.

There were three readers of it -- _build_chain, hf_helpers, _deploy_chain --
and moving it off admin.Versions took three separate discoveries, each one
found by a build failing in a new way. This module exists so the next move
is one edit.

Not admin: Atlas refuses writes to the admin database through any custom
role, deliberately, since admin is the security database. A counter living
there can never be bumped by an identity we are able to define; it only
worked while atlasAdmin accounts existed.

And DevOpsUser, not the application's connection string. Building is a
devops act, and routing it through MONGO_FRONTEND_connectionString is how
making a build succeed turned into widening the front end's grants.
"""
from __future__ import annotations

import sys
from pathlib import Path

VERSIONS_DB = "pipelineAdmin"
VERSIONS_COLLECTION = "Versions"


def _repo_root() -> Path:
    p = Path(__file__).resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    raise RuntimeError(f"no .git found walking up from {__file__}")


def versions_collection():
    """The Versions collection, as DevOpsUser."""
    lib_src = _repo_root() / "FrontEndApplicationLib" / "src"
    if str(lib_src) not in sys.path:
        sys.path.insert(0, str(lib_src))
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    return (ChatHealthyMongoUtilities()
            .getConnection("DevOpsUser", "admin")[VERSIONS_DB][VERSIONS_COLLECTION])


def latest_record() -> dict:
    """The newest Versions document. Callers that need version or framework
    alongside the counter read it once from here rather than opening their
    own connection."""
    latest = versions_collection().find_one(sort=[("from", -1)])
    if latest is None:
        sys.exit(f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} has no records.")
    return latest


def read_build_number() -> int:
    """The current global counter. One int shared across envs per
    build_deploy_promote_plan v3 (§3); per-env slots were removed."""
    latest = versions_collection().find_one(sort=[("from", -1)])
    if latest is None:
        sys.exit(f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} has no records.")
    build = latest.get("build")
    if build is None:
        sys.exit(
            f"ERROR: {VERSIONS_DB}.{VERSIONS_COLLECTION} latest record has "
            f"no 'build' field."
        )
    return int(build)
