# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Front-end serving-collection indexes, created by the deploy.

EPIC-008-F-012-S-004-REQ-B-013: a target's indexes are its own declared set,
created by the deploy. These are the front-end provider-search indexes; the
pipeline's indexes are separate and are NOT imposed here.

The provider search filters practice_addresses on {state, county.name} and on
{state, city}, both carrying the address collation (locale en, strength 2)
because those fields vary in case -- so an index the planner can seek on MUST
carry the same collation or it falls back to a scan. A named-provider search
filters the top-level legal-name fields, uppercased, so that index carries no
collation. provider_search_service names these three indexes.

Created idempotently by name, as the deploy's DevOpsUser identity, which
holds CREATE_INDEX (and only that) on the serving database.
"""
from __future__ import annotations

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()

ATLAS_INDEX_PACKAGE_KIND = "mongo_indexes"

_ADDRESS_COLLATION = {"locale": "en", "strength": 2}

FRONTEND_PROVIDER_INDEXES = [
    {
        "keys": [("practice_addresses.state", 1),
                 ("practice_addresses.county.name", 1)],
        "name": "idx_practice_state_county",
        "collation": _ADDRESS_COLLATION,
    },
    {
        "keys": [("practice_addresses.state", 1),
                 ("practice_addresses.city", 1)],
        "name": "idx_practice_state_city_ci",
        "collation": _ADDRESS_COLLATION,
    },
    {
        "keys": [("provider_last_name_legal_name", 1),
                 ("provider_first_name", 1),
                 ("provider_middle_name", 1)],
        "name": "idx_provider_name",
    },
]


def ensure_frontend_provider_indexes(provider_coll) -> list[dict]:
    """Create the declared front-end provider indexes on provider_coll,
    idempotently. The caller supplies a collection handle already opened on
    the serving cluster with an identity that holds CREATE_INDEX. Returns one
    result dict per index."""
    existing: dict = {}
    try:
        existing = {ix.get("name"): ix for ix in provider_coll.list_indexes()}
    except Exception as exc:  # noqa: BLE001 - absent list just means create all
        log.warning("ensure_frontend_provider_indexes: list_indexes "
                    "failed (%s); attempting creates anyway", exc)

    results: list[dict] = []
    for spec in FRONTEND_PROVIDER_INDEXES:
        name = spec["name"]
        if name in existing:
            # Presence is not correctness. An index the query cannot seek on
            # is worse than absent, because it reads as done. Check the keys
            # and the collation actually carried against what is declared;
            # a mismatch is reported, not silently accepted (a drop+recreate
            # to fix it needs a privilege this identity does not hold).
            ok, why = _index_matches(existing[name], spec)
            results.append({"name": name, "already_existed": True,
                            "shape_ok": ok, "shape_detail": why})
            if not ok:
                log.warning("ensure_frontend_provider_indexes: %s present but "
                            "WRONG SHAPE: %s", name, why)
            continue
        kwargs = {"name": name, "background": True}
        if spec.get("collation"):
            kwargs["collation"] = spec["collation"]
        created = provider_coll.create_index(spec["keys"], **kwargs)
        results.append({"name": created, "already_existed": False,
                        "shape_ok": True, "shape_detail": "created to spec"})
        log.info("ensure_frontend_provider_indexes: created %s", created)
    return results


def _index_matches(actual: dict, spec: dict) -> tuple[bool, str]:
    """Whether an existing index carries the declared keys and collation."""
    want_keys = [[k, d] for (k, d) in spec["keys"]]
    got_keys = [[k, d] for k, d in (actual.get("key") or {}).items()]
    if got_keys != want_keys:
        return False, f"keys {got_keys} != declared {want_keys}"
    want_coll = spec.get("collation")
    got_coll = actual.get("collation") or None
    if want_coll:
        if not got_coll:
            return False, ("declared collation "
                           f"{want_coll} but the index carries none")
        if (got_coll.get("locale") != want_coll["locale"]
                or got_coll.get("strength") != want_coll["strength"]):
            return False, (f"collation locale/strength "
                           f"{got_coll.get('locale')}/{got_coll.get('strength')}"
                           f" != declared {want_coll}")
    elif got_coll:
        return False, f"index carries collation {got_coll} but none is declared"
    return True, "keys and collation match"


def _provider_collection_from_arch(arch: dict) -> tuple[str, str]:
    """(db, collection) for the served provider collection, from the record's
    PROVIDER_COLLECTION binding. One shared versioned collection, so the first
    binding names it."""
    stack = [arch]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if (cur.get("collection_environment_name") == "PROVIDER_COLLECTION"
                    and cur.get("collection_base")
                    and cur.get("version") is not None):
                db, coll = cur["collection_base"].split(".", 1)
                return db, f"{coll}_v_{cur['version']}"
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    raise ChatHealthyException(
        mode="provider_binding_absent",
        component="ensure_frontend_indexes",
        message="no PROVIDER_COLLECTION binding in the deployment record")


def _manifest() -> dict:
    """The deployment record, read from disk. The served collection's
    database, name and version are declared there, so the index build reads
    the collection it must not guess."""
    import json
    import pathlib
    here = pathlib.Path(__file__).resolve()
    for parent in here.parents:
        if (parent / ".git").exists():
            path = (parent / "brain" / "machine_artifacts" / "content"
                    / "deployment_architecture.json")
            return json.loads(path.read_text(encoding="utf-8"))
    raise ChatHealthyException(
        mode="manifest_incomplete",
        component="ensure_frontend_indexes",
        message="no repository root above this file, so "
                "deployment_architecture.json cannot be located")


def build_provider_indexes(identity: str, cluster: str,
                           host: str = "") -> list[dict]:
    """Deploy entry point for the front-end index package. Ensures the
    declared provider indexes on the served collection, connecting as the
    identity the manifest names as the cluster's mongo_write consumer -- which
    holds CREATE_INDEX on the serving database. The served collection
    (database, name and version) is read from the deployment record, never
    hardcoded. manage_versions=False so the versioned collection name is
    addressed directly."""
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    db_name, coll_name = _provider_collection_from_arch(_manifest())
    client = ChatHealthyMongoUtilities(manage_versions=False).getConnection(
        identity, cluster, host=host)
    results = ensure_frontend_provider_indexes(client[db_name][coll_name])
    log.info("build_provider_indexes: %s.%s as %s@%s -> %s",
             db_name, coll_name, identity, cluster, results)
    return results
