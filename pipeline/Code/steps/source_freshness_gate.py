# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""source_freshness_gate — version-check every run, reuse when unchanged.

Per Skip 2026-07-29 (design intent): "check every time. look at the
current version (the date issued or other metadata that tells you when
the file was last updated) and download if and only if it is a new
version. sources sometimes update out-of-band" — so the check must be
source-side (ask the origin what version it publishes) on every run,
not TTL-based against our own clock.

For each source in ctx.config['source_freshness']:
  1. Probe the source: HEAD the URL, read ETag / Last-Modified /
     Content-Length; compose a version identifier.
  2. Read admin.DataSourceRegistry[source_name].version — the identifier
     we stored the last time we archived this source.
  3. If probe.identifier == registry.version AND the archived blob is
     still present: decision = "reuse". Downstream fetch skips download
     and returns the archived blob's coordinates so staging_load reads
     from archive.
  4. Else: decision = "fetch". Normal download + archive + registry
     update.

For pl_pfile (derived from nppes_npi's ZIP), the decision inherits
from nppes_npi.

The output is written to ctx.manifest.metrics['source_freshness']
as a per-source dict — fetch_all_sources reads it and threads
freshness_decision into each source's spec.
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

import os

from source_freshness_probe import probe_source_version

_log = ChatHealthyLoggingService()


# source_name -> env var name holding the source URL. Kept in sync with
# steps/fetch_all_sources._SOURCE_URL_ENVS.
_SOURCE_URL_ENVS = {
    "nppes_npi": "SOURCE_URL_NPPES_NPI",
    "nucc": "SOURCE_URL_NUCC",
    "census_zcta_county": "SOURCE_URL_CENSUS_ZCTA_COUNTY",
    "usda_rucc": "SOURCE_URL_USDA_RUCC",
    "specialty_catalog": "SPECIALTY_CLASSIFICATION_CATALOG_URL",
}
# Derived sources have no URL of their own; their freshness follows the
# parent. Kept in sync with steps/fetch_all_sources._DERIVED_SOURCES.
_DERIVED_PARENT = {
    "pl_pfile": "nppes_npi",
}


def _archived_blob_exists(mongo, source_name: str) -> bool:
    """Cheap presence check on the archived blob for this source per its
    DataSourceRegistry row. Freshness reuse requires both (a) the source
    hasn't changed AND (b) we actually still have the last-archived
    bytes to reuse."""
    doc = mongo["admin"]["DataSourceRegistry"].find_one({"source_name": source_name}) or {}
    return bool(doc.get("archive_blob"))


def execute(ctx) -> dict:
    from pipeline_db import get_mongo

    mongo = ctx.mongo_client or get_mongo()
    registry = mongo["admin"]["DataSourceRegistry"]
    freshness_list = ctx.config.get("source_freshness") or []
    # `source_freshness` may be a list of {source_name, ...} entries
    # (matches Mongo pipeline.config shape). We only care about the
    # source_name keys — TTL fields, if any, are ignored (design is
    # source-side version check, not TTL).
    configured = [e.get("source_name") for e in freshness_list if isinstance(e, dict) and e.get("source_name")]
    if not configured:
        # Default to the full known-source list when config carries no
        # freshness entries. Empty here previously caused every run to
        # download every source.
        configured = list(_SOURCE_URL_ENVS.keys()) + list(_DERIVED_PARENT.keys())

    decisions: dict[str, dict] = {}
    for name in configured:
        if name in _DERIVED_PARENT:
            # Skip — resolved below after parents are decided.
            continue
        env_key = _SOURCE_URL_ENVS.get(name)
        if not env_key:
            decisions[name] = {"decision": "fetch", "reason": f"no _SOURCE_URL_ENVS mapping for {name}"}
            continue
        url = os.environ.get(env_key, "").strip()
        if not url:
            decisions[name] = {"decision": "fetch", "reason": f"env var {env_key} not set"}
            continue
        try:
            current = probe_source_version(name, url)
        except Exception as exc:  # noqa: BLE001
            # Probe failure -> fetch (safe default: download if we can't tell).
            decisions[name] = {
                "decision": "fetch",
                "reason": f"probe failed: {type(exc).__name__}: {exc}",
                "current_version": None,
                "archived_version": None,
            }
            continue
        reg = registry.find_one({"source_name": name}) or {}
        # Compare against source_version_identifier (the probe-side
        # identifier we stored last archive). Fall back to the legacy
        # `version` field only if source_version_identifier is missing
        # — that legacy field is the SHA256 of the file bytes and does
        # NOT match probe output (etag/lastmod), so it will always miss
        # and fall through to "fetch" on the first run after this lands
        # (correct behaviour: refetch once so we can record the probe
        # identifier, then reuse forever after until the source changes).
        archived = reg.get("source_version_identifier")
        archive_present = bool(reg.get("archive_blob"))
        if archived and current == archived and archive_present:
            decisions[name] = {
                "decision": "reuse",
                "current_version": current,
                "archived_version": archived,
                "archive_container": reg.get("archive_container"),
                "archive_blob": reg.get("archive_blob"),
                "archive_filename": reg.get("filename"),
            }
        else:
            decisions[name] = {
                "decision": "fetch",
                "current_version": current,
                "archived_version": archived,
                "reason": (
                    "no prior archive" if not archived
                    else "archive blob missing" if not archive_present
                    else "source version changed"
                ),
            }

    # Derived sources inherit from parent.
    for derived, parent in _DERIVED_PARENT.items():
        parent_dec = decisions.get(parent)
        if parent_dec:
            decisions[derived] = {"decision": parent_dec["decision"], "inherited_from": parent}
        else:
            decisions[derived] = {"decision": "fetch", "reason": f"parent {parent} not decided"}

    ctx.manifest.metrics["source_freshness"] = decisions
    _log.info("source_freshness_gate: decisions=%s", {k: v.get("decision") for k, v in decisions.items()})
    return {"decisions": decisions}


def run_step(ctx) -> dict:
    return execute(ctx)
