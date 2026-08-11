# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""fetch_all_sources — one Worker per source, per Skip's design.

Controller does not fetch. This step is process_pool-fanned: each partition
is one source_name; the base orchestrator spawns one Worker subprocess per
partition; each Worker fetches its one source and returns. Workers run in
parallel; Controller returns when all are done.

Every Worker reads the URL for its assigned source from a plain env var
propagated through the standard deploy chain (declared in
deployment_architecture.json, sourced from .env, pushed to KV +
Automation Variable, hydrated into the Controller container which passes
it to each Worker subprocess via env inheritance):

  nppes_npi          -> SOURCE_URL_NPPES_NPI
  pl_pfile           -> derived (extracted from nppes_npi zip inside the worker)
  nucc               -> SOURCE_URL_NUCC
  census_zcta_county -> SOURCE_URL_CENSUS_ZCTA_COUNTY
  usda_rucc          -> SOURCE_URL_USDA_RUCC
"""

from __future__ import annotations
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import os

from source_fetch_engine import fetch_all_sources

_log = ChatHealthyLoggingService()

# Direct-URL sources: env var name -> source name. pl_pfile is derived
# from nppes_npi's downloaded zip (no separate URL).
_SOURCE_URL_ENVS = {
    "nppes_npi": "SOURCE_URL_NPPES_NPI",
    "nucc": "SOURCE_URL_NUCC",
    "census_zcta_county": "SOURCE_URL_CENSUS_ZCTA_COUNTY",
    "usda_rucc": "SOURCE_URL_USDA_RUCC",
    "specialty_catalog": "SPECIALTY_CLASSIFICATION_CATALOG_URL",
}
_DERIVED_SOURCES = {
    "pl_pfile": {"derived_from": "nppes_npi", "zip_entry_glob": "pl_pfile*.csv"},
}


def _source_spec_from_env(source_name: str) -> dict:
    if source_name in _DERIVED_SOURCES:
        return dict(_DERIVED_SOURCES[source_name])
    env_key = _SOURCE_URL_ENVS.get(source_name)
    if not env_key:
        raise ChatHealthyException(
            mode="runtime_error",
            message=f"fetch_all_sources: no env var mapping for source {source_name!r}",
            component="fetch_all_sources",
            source_name=source_name,
        )
    url = os.environ.get(env_key, "").strip()
    if not url:
        raise ChatHealthyException(
            mode="runtime_error",
            message=f"fetch_all_sources: env var {env_key} is not set (needed for source {source_name!r})",
            component="fetch_all_sources",
            source_name=source_name,
            env_key=env_key,
        )
    return {"source_url": url}


def run_step(ctx) -> dict:
    """Worker path: fetch ONE source, identified by ctx.config['partition']['source'].

    When source_name is nppes_npi, the Worker ALSO derives every
    _DERIVED_SOURCES entry whose derived_from == nppes_npi (currently
    pl_pfile) from the same downloaded zip. This is the only correct
    ordering because derived sources cannot run as their own Workers
    (the parent's downloaded blob is not visible across Worker
    processes).
    """
    partition = ctx.config.get("partition") or {}
    source_name = partition.get("source") if isinstance(partition, dict) else None
    if not source_name:
        raise ChatHealthyException(
            mode="runtime_error",
            message="fetch_all_sources: expected ctx.config['partition']['source'] to name the source this worker owns",
            component="fetch_all_sources",
            partition=partition,
        )
    freshness = ctx.manifest.metrics.get("source_freshness") or {}
    spec = _source_spec_from_env(source_name)
    # freshness value is either a legacy string (fetch|reuse) or a dict
    # with decision + archive coords per the source_freshness_gate v2
    # contract. Normalise to the dict form and copy archive coords into
    # the spec so _fetch_one_source can honor reuse without another DB
    # roundtrip.
    def _apply_freshness(spec_dict: dict, fresh_val) -> dict:
        if isinstance(fresh_val, dict):
            spec_dict.setdefault("freshness_decision", fresh_val.get("decision", "fetch"))
            # source_freshness_gate writes 'archive_blob' (not
            # 'archive_blob_path'); the earlier key-name mismatch caused
            # reuse-decision sources to lose their archive coords and be
            # silently skipped by source_fetch_engine.
            for k in ("archive_container", "archive_blob", "archive_filename", "archived_version"):
                if fresh_val.get(k) is not None:
                    spec_dict[k] = fresh_val[k]
        else:
            spec_dict.setdefault("freshness_decision", fresh_val or "fetch")
        return spec_dict
    spec = _apply_freshness(spec, freshness.get(source_name))

    sources: dict = {source_name: spec}
    for derived_name, derived_spec in _DERIVED_SOURCES.items():
        if derived_spec.get("derived_from") == source_name:
            child = dict(derived_spec)
            child = _apply_freshness(child, freshness.get(derived_name))
            sources[derived_name] = child

    config = {
        "run_id": ctx.run_id,
        "env": ctx.env_prefix,
        "transient_container": f"{ctx.env_prefix}-pipeline-transients",
        "sources": sources,
    }
    result = fetch_all_sources(
        config,
        mongo=ctx.mongo_client,
        blob=ctx.blob_client,
    ) or {}

    ctx.manifest.source_versions.update(result.get("source_versions") or {})
    fetch_results = ctx.manifest.metrics.setdefault("fetch_results", {})
    for r in result.get("results") or []:
        fetch_results[r["source_name"]] = r
    return {"source": source_name, **result}


def execute(ctx):
    return run_step(ctx)
