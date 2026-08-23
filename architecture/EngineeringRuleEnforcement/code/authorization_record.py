"""The one place an authorization is written.

Design V31 section 4.14, table 19. Every authorization -- a commit, a
promotion, a data migration -- is one document in a single collection, told
apart by authorization_type. There is one collection because an auditor
asking "what did a human approve, and when" should have one place to look,
and one writer because two writers become two shapes.

The collection is outside the repository the rules govern. A gate that
recorded its verdicts into a file inside the tree it polices is asking the
governed thing to hold its own evidence, and a commit can rewrite that file
in the same breath as the thing it was gating.

Deliberately no sys.path manipulation here: this module is imported by
workers that have already resolved chathealthy_lib, and a library that
searches the filesystem for its own dependencies is the condition A-5
describes.
"""
from __future__ import annotations

import json
import pathlib

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

_log = ChatHealthyLoggingService()

# The design names GovernanceAdminDb. deployment_architecture.json declares
# DevOpsAdmin and no GovernanceAdminDb, and the identity's grants are written
# against the declared name. The record the deploy can prove wins over the
# name in prose; the divergence is the operator's to settle.
AUTHORIZATION_DATABASE = "DevOpsAdmin"
AUTHORIZATION_COLLECTION = "Authorizations"
AUTHORIZATION_TARGET = "target_atlas_frontend"

_collection = None


def _manifest() -> dict:
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            path = (parent / "brain" / "machine_artifacts" / "content"
                    / "deployment_architecture.json")
            return json.loads(path.read_text(encoding="utf-8"))
    raise ChatHealthyException(
        "manifest_incomplete",
        "no repository root above this file, so deployment_architecture.json "
        "cannot be located and the authorization has no cluster to reach")


def deployment_facts(env: str = "dev") -> tuple[str, str]:
    """The cluster written to and the identity written as.

    Read from the record that owns them. A deployment fact restated in code
    is a fact the record can no longer be trusted to describe.
    """
    record = _manifest()
    target = next((t for t in record["DeploymentTargetRecord"]
                   if t["target_id"] == AUTHORIZATION_TARGET), None)
    if target is None:
        raise ChatHealthyException(
            "manifest_incomplete",
            f"{AUTHORIZATION_TARGET} is not declared; the authorization has "
            f"no cluster to be written to")
    binding = next((e for e in target["environments"]
                    if e["env_binding"] == env), target["environments"][0])
    cluster = binding["atlas"]["cluster"]
    consumers = cluster.get("runtime_consumers") or []
    if not consumers:
        raise ChatHealthyException(
            "manifest_incomplete",
            f"{AUTHORIZATION_TARGET} declares no runtime consumer; the "
            f"authorization has no identity to be written as")
    identity_target = consumers[0]["identity_target_id"]
    identity = next(
        (i["identity_id"] for i in record["IdentityCatalog"]
         if i.get("target_id_ref") == identity_target), None)
    if identity is None:
        raise ChatHealthyException(
            "manifest_incomplete",
            f"{identity_target} names no identity in the IdentityCatalog")
    return cluster["cluster_name"], identity


def _load_env_beside_the_repository() -> None:
    """Put .env on the environment for values not already set.

    The gates run in a container with the repository mounted, and the
    credentials that reach Atlas live in .env at its root -- not in git, and
    not in the container's own environment. Without this the write failed on
    KEY_VAULT_URI and every verdict went unrecorded while the gate reported
    itself working.

    Existing values win. The image sets CH_LOG_DESTINATION deliberately, and
    the application .env sets it to mongo; letting the file override would
    point devops logging at a database this identity cannot write to.
    """
    import os  # noqa: PLC0415

    for parent in pathlib.Path(__file__).resolve().parents:
        if not (parent / ".git").exists():
            continue
        env_file = parent / ".env"
        if not env_file.is_file():
            return
        from dotenv import dotenv_values  # noqa: PLC0415
        for name, value in dotenv_values(env_file).items():
            if value is not None and name not in os.environ:
                os.environ[name] = value
        return


def collection():
    global _collection
    if _collection is None:
        _load_env_beside_the_repository()
        cluster, identity = deployment_facts()
        client = ChatHealthyMongoUtilities().getConnection(identity, cluster)
        _collection = client[AUTHORIZATION_DATABASE][AUTHORIZATION_COLLECTION]
    return _collection


def _report_unrecorded(document: dict, exc: Exception) -> None:
    """Say that a verdict happened and left no record.

    Separate from append() because a function that raises does not also log
    -- the catcher logs, not the thrower -- and append() raises.
    """
    _log.error(
        f"[authorization] the {document['authorization_type']} record could "
        f"not be written to {AUTHORIZATION_DATABASE}.{AUTHORIZATION_COLLECTION}"
        f": {exc}. The verdict stands; the record of it does not.")
    return None


def append(document: dict, tolerate_failure: bool):
    """Add a document. Never alters one.

    tolerate_failure is the difference between the two gates, and it is a
    difference in what the record means rather than in how carefully it is
    written. A promotion that could not be recorded must not proceed: the
    record is the proof the requirement asks for, and a promotion with no
    proof did not happen. A commit is different -- the operator has already
    approved it, refusing it now would make the ability to commit depend on a
    database being reachable, and the verdict is already on stderr. So the
    commit tolerates the failure and says so.
    """
    try:
        return collection().insert_one(document).inserted_id
    except Exception as exc:  # noqa: BLE001 - converted at the boundary
        if tolerate_failure:
            return _report_unrecorded(document, exc)
        raise ChatHealthyException(
            "authorization_unrecordable",
            f"the authorization could not be written to "
            f"{AUTHORIZATION_DATABASE}.{AUTHORIZATION_COLLECTION}: {exc}. It "
            f"does not proceed; an authorization that left no record did not "
            f"occur.",
            component="authorization_record",
            exception=exc)
