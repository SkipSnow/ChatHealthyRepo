"""Which of the library's capabilities a process is allowed to load.

One library is installed into every container, so every container carries
every capability the library has -- including the ones that reach the
pipeline and name pipelineEditor. A front-end process had no way to be told
it may not use them, and `getConnection(identity, cluster)` takes the
identity as an argument, so any module in the process could ask for any
identity by name.

The permission is enforced at import, by a finder on sys.meta_path. A module
this component may not use never loads: not a wrapper that can be reached
around, and a late import inside a function is caught the same as one at the
top of a file.

Order matters and is the whole design:

  1. The component's name arrives in the process environment, stamped there
     by the deploy from deployment_architecture.json. It is not a literal in
     any file, because the library is one artefact shipped identically to
     every container and cannot hold a different name for each.
  2. Library init installs the finder DENYING everything, with exactly one
     allow: getLibPermissions. Nothing else can load yet.
  3. getLibPermissions reads this component's exclusion list. It is readable
     by any identity -- knowing the policy grants nothing.
  4. The finder swaps to the real list.

There is no window in which a forbidden module is resident before the policy
is known, which there would be if the policy were fetched first and enforced
afterwards.

The list is an exclusion list, not an inclusion list, because most of the
library is shared by every component and enumerating what each may use would
be a list nobody maintains. The consequence is chosen, not stumbled into: a
capability added to the library next month is permitted everywhere until
someone excludes it.

Write access to the collection is the operator's. This module only reads.
"""
from __future__ import annotations

import os
import sys
from importlib.abc import MetaPathFinder
from typing import Optional, Sequence

from .exceptions import ChatHealthyException

COMPONENT_ENV_VAR = "CH_COMPONENT"
# The identity this process authenticates as. A component is what the process
# IS; an identity is who it presents itself as, and they are not the same
# name -- findcare runs as frontendUser. Both are declared per target and
# stamped by the deploy, because the library is one artefact and can hold
# neither.
IDENTITY_ENV_VAR = "CH_IDENTITY"
PERMISSIONS_DB = "ChatHealthyConfig"
PERMISSIONS_COLLECTION = "LibPermissions"

# What is reachable before the policy is known: getLibPermissions and the
# capabilities that one method needs to reach the collection. Denying those
# denies the process the ability to learn what it may do, which is how the
# first version of this failed -- it blocked mongo_utilities and then asked
# Mongo for the answer.
#
# These four are therefore loadable in every component whatever the exclusion
# list says. That is a property of having to fetch the policy over the same
# machinery the policy governs, and it is stated here rather than discovered:
# a component cannot be denied the connection utility, the exception type, or
# the logger, because the refusal itself needs all three.
_BOOTSTRAP_ALLOW = frozenset({
    "chathealthy_lib.permissions",
    "chathealthy_lib.exceptions",
    "chathealthy_lib.mongo_utilities",
    "chathealthy_lib.logging_service",
    # mongo_utilities resolves a versioned collection name through this, so
    # the read cannot complete without it.
    "chathealthy_lib.runtime_data_collections",
})

_PACKAGE = __name__.rsplit(".", 1)[0]

_state: dict = {"component": None, "excluded": None, "installed": False}


class _ExclusionFinder(MetaPathFinder):
    """Refuses to find a module this component may not use.

    Returning None from find_spec would hand the import on to the next
    finder, which is what every other finder on the path does when a module
    is not theirs. This one raises instead: a refusal that falls through is
    not a refusal.
    """

    def __init__(self, excluded: Sequence[str], bootstrap_only: bool = False):
        self._excluded = set(excluded)
        self._bootstrap_only = bootstrap_only

    def find_spec(self, fullname, path=None, target=None):
        if not fullname.startswith(_PACKAGE + "."):
            return None
        if self._bootstrap_only:
            if fullname in _BOOTSTRAP_ALLOW:
                return None
            raise ChatHealthyException(
                mode="lib_permission_not_yet_known",
                component="chathealthy_lib.permissions",
                message=f"{fullname} was imported before the permission list "
                        f"was read. Library initialisation must complete "
                        f"before any other capability is loaded.")
        leaf = fullname.rsplit(".", 1)[-1]
        if fullname in self._excluded or leaf in self._excluded:
            raise ChatHealthyException(
                mode="lib_capability_forbidden",
                component="chathealthy_lib.permissions",
                message=f"{fullname} is not a capability "
                        f"{_state['component']!r} may use. The exclusion list "
                        f"is held in {PERMISSIONS_DB}.{PERMISSIONS_COLLECTION} "
                        f"and is the operator's to change.")
        return None


def component_name() -> str:
    """This process's business component, from the environment.

    Absent is fatal. A default here is how one container silently becomes
    another and loads the capabilities of a component it is not.
    """
    name = (os.environ.get(COMPONENT_ENV_VAR) or "").strip()
    if not name:
        raise ChatHealthyException(
            mode="component_not_named",
            component="chathealthy_lib.permissions",
            message=f"{COMPONENT_ENV_VAR} is not set. The deploy stamps it "
                    f"from deployment_architecture.json; a process that "
                    f"cannot say which component it is cannot be told which "
                    f"capabilities it may use.")
    return name


def getLibPermissions() -> list[str]:
    """The capabilities this component may NOT use.

    Read from the collection, never from deployment_architecture.json: the
    record is written by the actor the policy constrains. Readable by any
    identity, writable by the operator alone.

    An unreadable list is fatal. Continuing with an empty exclusion list
    would grant every capability at the moment the control failed, which is
    the one moment it must not.
    """
    name = component_name()
    identity = (os.environ.get(IDENTITY_ENV_VAR) or "").strip()
    if not identity:
        raise ChatHealthyException(
            mode="identity_not_named",
            component="chathealthy_lib.permissions",
            message=f"{IDENTITY_ENV_VAR} is not set. The deploy stamps it from "
                    f"deployment_architecture.json; the policy is read as the "
                    f"identity the process authenticates as, and a process "
                    f"that cannot name its identity cannot read it.")
    from .mongo_utilities import ChatHealthyMongoUtilities  # noqa: PLC0415
    try:
        client = ChatHealthyMongoUtilities().getConnection(
            identity, "ChatHealthyFrontEnd")
        row = client[PERMISSIONS_DB][PERMISSIONS_COLLECTION].find_one(
            {"component": name})
    except ChatHealthyException:
        raise
    except Exception as exc:  # noqa: BLE001 - converted at this boundary
        raise ChatHealthyException(
            mode="lib_permissions_unreadable",
            component="chathealthy_lib.permissions",
            message=f"the capability list for {name!r} could not be read from "
                    f"{PERMISSIONS_DB}.{PERMISSIONS_COLLECTION}: {exc}",
            exception=exc) from exc
    if row is None:
        raise ChatHealthyException(
            mode="lib_permissions_absent",
            component="chathealthy_lib.permissions",
            message=f"{PERMISSIONS_DB}.{PERMISSIONS_COLLECTION} carries no row "
                    f"for component {name!r}. A component the policy does not "
                    f"name is not a component that may load anything.")
    return [str(m) for m in (row.get("excluded_modules") or [])]


def initialize() -> str:
    """Establish this process's identity and what it may load.

    Called as the first statement of a component's entry point, before any
    other library capability is imported.
    """
    if _state["installed"]:
        return _state["component"]
    name = component_name()
    _state["component"] = name

    gate = _ExclusionFinder((), bootstrap_only=True)
    sys.meta_path.insert(0, gate)
    try:
        excluded = getLibPermissions()
    finally:
        sys.meta_path.remove(gate)

    _state["excluded"] = excluded
    sys.meta_path.insert(0, _ExclusionFinder(excluded))
    _state["installed"] = True
    return name


def excluded_capabilities() -> Optional[list[str]]:
    """What this process was told it may not use, or None before init."""
    return None if _state["excluded"] is None else list(_state["excluded"])
