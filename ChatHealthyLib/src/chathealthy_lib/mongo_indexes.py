# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Canonical MongoDB index application. One library, every consumer.

Index shapes are deployment content: they are declared in the record
(deployment_architecture.json, the one canonical place) and applied by this
one library, whether the collection lives on the front-end serving cluster or
a pipeline cluster. This module holds NO index fact of its own -- not a key,
not a collection, not a cluster. It is handed declarations and it applies them.

Two layers:
  apply_indexes(collection, specs)      the pure applier over one collection
  apply_index_catalog(entries, ...)     the driver over fully-qualified entries

A catalog entry is self-describing: {cluster, identity, db, collection,
indexes}. The driver connects to each entry's cluster as that entry's own
identity, so a misdeclared cluster or collection cannot pollute another: the
certificate that identity carries is denied at the door. Zero-trust inside
Mongo is what makes one shared builder safe across every cluster.

Create-only: an index is created when nothing satisfies it, left alone when
something does, and a name held by an index that does not cover the declaration
raises. Dropping an index is never this library's to do.
"""
from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()

# Field roots that are arrays on ChatHealthy documents. A compound index may
# span at most one of these (MongoDB rejects a compound multikey index over two
# array paths, error 171 "cannot index parallel arrays"), so an index whose
# keys touch two of them is refused here -- at declaration, naming the pair --
# rather than at first insert, where it once cost a 600,248-document scan.
_ARRAY_FIELD_ROOTS = frozenset({
    "taxonomies",
    "practice_addresses",
    "addresses",
    "active",
    "other_identifiers",
})


def _array_root(field: str) -> str | None:
    root = field.split(".", 1)[0]
    return root if root in _ARRAY_FIELD_ROOTS else None


def _validate_no_parallel_arrays(spec: dict) -> None:
    roots = {_array_root(field) for field, _order in spec["keys"]}
    roots.discard(None)
    if len(roots) > 1:
        raise ChatHealthyException(
            mode="index_parallel_array",
            component="mongo_indexes",
            message=f"index {spec['name']!r} spans two array paths "
                    f"{sorted(roots)}; a compound index cannot cover two arrays. "
                    f"Split it into one index per array.")


def _satisfies(actual: dict, spec: dict) -> bool:
    """Whether an existing index covers the declared spec: the declared keys are
    a leading prefix of the index's keys (an extra trailing key is fine, a query
    seeks the prefix) and the declared collation is carried."""
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


def apply_indexes(collection, index_specs: list[dict]) -> list[dict]:
    """Ensure each declared index exists on one collection handle. Idempotent.

    index_specs is business content: one dict per index --
    {name, keys: [[field, order], ...], unique?, sparse?, collation?}. The
    caller supplies a collection handle opened as an identity holding
    CREATE_INDEX on that database.

    An existing index satisfies a declared one when the declared keys are a
    leading prefix of its keys and it carries the declared collation; a
    satisfied index is left as is. A declared index nothing satisfies is
    created. A name already held by an index that does NOT cover the
    declaration raises: that is drift, and this library never drops.
    """
    existing = {ix.get("name"): ix for ix in collection.list_indexes()}
    results: list[dict] = []
    for spec in index_specs:
        _validate_no_parallel_arrays(spec)
        name = spec["name"]
        if name in existing:
            if _satisfies(existing[name], spec):
                results.append({"name": name, "already_satisfied": True})
                continue
            raise ChatHealthyException(
                mode="index_conflict",
                component="mongo_indexes",
                message=f"index {name!r} already exists on {collection.full_name} "
                        f"but does not cover the declared keys and collation. "
                        f"This is drift; dropping is not this library's to do.")
        keys = [(field, order) for field, order in spec["keys"]]
        kwargs = {"name": name, "background": True}
        for option in ("unique", "sparse", "collation"):
            if spec.get(option) is not None:
                kwargs[option] = spec[option]
        created = collection.create_index(keys, **kwargs)
        results.append({"name": created, "created": True})
    log.info("apply_indexes: %s -> %s", collection.full_name, results)
    return results


def apply_index_catalog(entries: list[dict], *, resolve_collection,
                        host_for=None) -> list[dict]:
    """Apply a list of fully-qualified index-catalog entries.

    entries            [{cluster, identity, db, collection, indexes, ...}, ...]
    resolve_collection(entry) -> the concrete collection name to apply to. The
                       caller owns version authority -- the front-end deploy
                       resolves the versioned name from the DBVersions binding,
                       the pipeline from the run's data_version -- so this module
                       never reads the version map (the pipeline container has no
                       manifest to read it from).
    host_for(cluster) -> the host string, or None to let getConnection resolve
                       it (the container path, where the host is a cert fact;
                       the deploy path passes cluster_host.host_for).

    Connects per entry as that entry's identity, so each write is authorized by
    that identity's certificate and nothing else.
    """
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: PLC0415
    out: list[dict] = []
    for entry in entries:
        coll_name = resolve_collection(entry)
        host = host_for(entry["cluster"]) if host_for else ""
        client = ChatHealthyMongoUtilities(manage_versions=False).getConnection(
            entry["identity"], entry["cluster"], host=host)
        results = apply_indexes(client[entry["db"]][coll_name], entry["indexes"])
        out.append({
            "catalog_id": entry.get("catalog_id"),
            "cluster": entry["cluster"],
            "db": entry["db"],
            "collection": coll_name,
            "results": results,
        })
        log.info("apply_index_catalog: %s -> %s.%s as %s@%s",
                 entry.get("catalog_id"), entry["db"], coll_name,
                 entry["identity"], entry["cluster"])
    return out
