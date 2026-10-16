# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The Medicare run's provider source, named explicitly rather than derived.

The collections this run builds carry their own data versions, independent of
the Provider collection it joins against. So the provider source is named by
the operator as a fully-qualified ``cluster.db.collection`` argument
(``providerCollection``) instead of being derived from the run's data_version.

parse_provider_source validates the argument at the very start of the run:
absent, malformed, or not a Pipeline collection in PipelinePublicHealthData on
the pipeline cluster all abend immediately. confirm_provider_source_exists adds
the existence check once the pipeline cluster is reachable, so a mangled name or
a missing collection stops the run at the beginning -- never after the work.
"""
from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException

PROVIDER_CLUSTER = "ChatHealthyDataPipelines"
PROVIDER_DB = "PipelinePublicHealthData"


def parse_provider_source(provider_collection) -> tuple[str, str]:
    """Return (db, collection) from a 'cluster.db.collection' string.

    Abends at once if the argument is absent, malformed, or does not name a
    Pipeline collection in PipelinePublicHealthData on the pipeline cluster.
    """
    raw = (provider_collection or "").strip()
    if not raw:
        raise ChatHealthyException(
            mode="missing_resources", component="provider_source",
            message=("providerCollection is required and names the provider "
                     "source as cluster.db.collection. It is a resource the "
                     "workbook needs; fire again with a qualified value."))
    parts = [p.strip() for p in raw.split(".")]
    if len(parts) != 3 or not all(parts):
        raise ChatHealthyException(
            mode="value_error", component="provider_source",
            message=(f"providerCollection must be cluster.db.collection; "
                     f"got {raw!r}."))
    cluster, db, collection = parts
    if cluster != PROVIDER_CLUSTER or db != PROVIDER_DB:
        raise ChatHealthyException(
            mode="value_error", component="provider_source",
            message=(f"providerCollection must name a Pipeline collection in "
                     f"{PROVIDER_CLUSTER}.{PROVIDER_DB}; got {raw!r}."))
    return db, collection


def confirm_provider_source_exists(mongo, provider_collection) -> None:
    """Confirm the named provider collection exists, abending if it does not.

    ``mongo`` is a client on the pipeline cluster. Called once the cluster is
    awake so a missing collection stops the run at the beginning.
    """
    db, collection = parse_provider_source(provider_collection)
    if collection not in mongo[db].list_collection_names():
        raise ChatHealthyException(
            mode="missing_resources", component="provider_source",
            message=(f"provider source {PROVIDER_CLUSTER}.{db}.{collection} "
                     f"does not exist; there is nothing to join against."))
