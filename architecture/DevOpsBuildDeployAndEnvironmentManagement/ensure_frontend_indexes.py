# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Front-end serving-collection indexes, applied by the deploy from the record.

EPIC-008-F-012-S-004-REQ-B-013: a target's indexes are its own declared set,
created by the deploy. This module holds NO fact of its own -- not the indexes,
not the collection they land on, not the package kind. Every fact is read from
the deployment record: the deploy handler hands this the mongo_indexes package's
config (the collection it targets and the index set), and this applies it, as
the identity the record names as the serving cluster's writer, which holds
CREATE_INDEX (and only that) on the serving database.

This deploy only ever CREATES an index. It never drops one and never creates
over an existing one: if a declared index name already exists on the collection,
that is a bug and it raises. Dropping an index is never the deploy's to do.
"""
from __future__ import annotations

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()


def ensure_provider_indexes(provider_coll, index_specs: list[dict]) -> list[dict]:
    """Ensure each declared index is served by the collection.

    An existing index satisfies a declared one when the declared keys are a
    LEADING PREFIX of its keys -- an extra trailing key is fine, the query seeks
    the prefix -- and it carries the declared collation; a satisfied index is
    left as is. A declared index nothing satisfies is created. A name already
    held by an index that does NOT cover the declaration is a bug and raises.

    index_specs is business content read from the deployment record: one dict
    per index -- {name, keys: [[field, order], ...], collation?}. The caller
    supplies a collection handle opened as an identity holding CREATE_INDEX.
    """
    existing = {ix.get("name"): ix for ix in provider_coll.list_indexes()}
    results: list[dict] = []
    for spec in index_specs:
        name = spec["name"]
        if name in existing:
            if _satisfies(existing[name], spec):
                results.append({"name": name, "already_satisfied": True})
                continue
            raise ChatHealthyException(
                mode="index_conflict",
                component="ensure_frontend_indexes",
                message=f"index {name!r} already exists but does not cover the "
                        f"declared keys and collation. This is a bug.")
        keys = [(field, order) for field, order in spec["keys"]]
        kwargs = {"name": name, "background": True}
        if spec.get("collation"):
            kwargs["collation"] = spec["collation"]
        created = provider_coll.create_index(keys, **kwargs)
        results.append({"name": created, "created": True})
    return results


def _satisfies(actual: dict, spec: dict) -> bool:
    """Whether an existing index covers the declared spec: the declared keys are
    a leading prefix of the index's keys (an extra trailing key is fine) and the
    declared collation is carried."""
    want = [[f, o] for f, o in spec["keys"]]
    got = [[k, d] for k, d in (actual.get("key") or {}).items()]
    if got[:len(want)] != want:
        return False
    want_coll = spec.get("collation")
    got_coll = actual.get("collation") or None
    if want_coll:
        return bool(got_coll
                    and got_coll.get("locale") == want_coll["locale"]
                    and got_coll.get("strength") == want_coll["strength"])
    return got_coll is None


def _collection_from_ref(arch: dict, collection_ref: str) -> tuple[str, str]:
    """(db, collection) for the collection the record names under
    collection_ref, resolving the versioned name from its binding."""
    stack = [arch]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            if (cur.get("collection_environment_name") == collection_ref
                    and cur.get("collection_base")
                    and cur.get("version") is not None):
                db, coll = cur["collection_base"].split(".", 1)
                return db, f"{coll}_v_{cur['version']}"
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
    raise ChatHealthyException(
        mode="collection_binding_absent",
        component="ensure_frontend_indexes",
        message=f"no {collection_ref!r} binding in the deployment record")


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
                           config: dict, host: str = "") -> list[dict]:
    """Deploy entry point for the front-end index package. Reads the target
    collection and the index set from the package config the handler passes --
    holding no fact of its own -- and applies the indexes, connecting as the
    identity the record names the cluster's writer, which holds CREATE_INDEX on
    the serving database. manage_versions=False so the versioned collection name
    is addressed directly."""
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    db_name, coll_name = _collection_from_ref(_manifest(), config["collection_ref"])
    client = ChatHealthyMongoUtilities(manage_versions=False).getConnection(
        identity, cluster, host=host)
    results = ensure_provider_indexes(client[db_name][coll_name], config["indexes"])
    log.info("build_provider_indexes: %s.%s as %s@%s -> %s",
             db_name, coll_name, identity, cluster, results)
    return results
