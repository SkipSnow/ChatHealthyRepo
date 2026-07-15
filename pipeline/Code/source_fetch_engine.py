# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Parallel source fetcher for the Provider Pipeline — LLD v23 §4.4.

Every external file the pipeline consumes lands in a run-scoped transient
blob location, verified by content hash, with a durable per-source version
identity that the archival step then promotes to
`{env}-pipeline-sources/{source_name}/{version}/`.

Realizes:

  - EPIC-010-F-102-S-003-REQ-B-001  data-source fetching
  - EPIC-010-F-102-S-003-REQ-B-002  per-source freshness TTL (in days)
  - EPIC-010-F-102-S-003-REQ-T-002  gather activity inventory
  - EPIC-010-F-102-S-003-REQ-T-003  source-gather concurrency bounded by throttle
  - EPIC-010-F-102-S-003-REQ-T-004  AI-agent source-URL discovery (NPPES, USDA RUCC)
  - EPIC-010-F-102-S-004-REQ-B-001  source-file storage management (version identity)
  - EPIC-010-F-103-S-002-REQ-B-001..REQ-B-005  NPPES base sources
  - EPIC-010-F-103-S-003  multi-address (pl_pfile) file
  - EPIC-010-F-103-S-004  Census ZCTA-to-County crosswalk
  - EPIC-010-F-103-S-005  USDA RUCC classification workbook

Source inventory (LLD §4.4). The engine fetches every source enumerated
here whose freshness-gate decision was "fetch". Sources whose decision
was "reuse" are passed through with a `skipped=True` record so the
archival step and the source_versions block still see them.

  nppes_npi          — CMS NPPES full monthly dissemination
  pl_pfile           — NPPES practice locations
  nucc               — NUCC provider taxonomy CSV
  census_zcta_county — U.S. Census ZCTA-to-County crosswalk
  usda_rucc          — USDA RUCC classification workbook
  specialty_catalog  — F-105 classification catalog (pipeline cluster)

Concurrency contract per REQ-T-003. Sources are fetched in parallel
using a thread pool bounded by `config["fetch_concurrency"]` (default 6).
Each vendor call is issued through a per-source RateLimitedGate whose
rate is drawn from `config["throttle_rates"][source_name]`. NPPES-registry
and Google-Maps rates land here for use by both the fetch step and the
county cascade.

URL discovery per REQ-T-004. Every source whose canonical URL is not
a stable string (NPPES monthly, USDA RUCC yearly, NUCC quarterly,
Census decennial) is resolved through source_url_discovery.find_latest_data_url
at fetch time. There is no fallback URL constant. On discovery failure
the fetch aborts and the pipeline fails loudly.

Content-hash + version-identity contract per S-004-REQ-B-001. Every
fetched file gets a SHA-256 computed on the wire. The tuple (source_name,
sha256) is the durable version identity; the archival step (§4.5)
promotes {run_id}/{source}/{filename} to
{source_name}/{version}/{filename}.

Public entry point: `fetch_all_sources(config, mongo, blob)`.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from source_url_discovery import find_latest_data_url
from throttle_semaphore import RateLimitedGate

_log = logging.getLogger(__name__)

DEFAULT_FETCH_CONCURRENCY = 6
DEFAULT_HTTP_TIMEOUT_SEC = 600
DEFAULT_CHUNK_BYTES = 1024 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_stream(fh) -> str:
    h = hashlib.sha256()
    while True:
        block = fh.read(DEFAULT_CHUNK_BYTES)
        if not block:
            break
        h.update(block)
    return h.hexdigest()


def _discover_url(source_name: str, discovery: dict) -> str:
    """LLD §4.4 REQ-T-004: AI-agent URL discovery, no fallback constants."""
    page_url = discovery.get("page_url")
    instructions = discovery.get("instructions", "")
    if not page_url:
        raise RuntimeError(
            f"source_fetch_engine[{source_name}]: url_discovery.page_url is required"
        )
    return find_latest_data_url(
        source_name=source_name,
        page_url=page_url,
        instructions=instructions,
    )


def _resolve_source_url(source_name: str, spec: dict) -> str:
    """Return the URL to fetch. Prefer discovery when configured."""
    if spec.get("url_discovery"):
        return _discover_url(source_name, spec["url_discovery"])
    url = spec.get("source_url")
    if not url:
        raise RuntimeError(
            f"source_fetch_engine[{source_name}]: no source_url or url_discovery configured"
        )
    return url


def _target_blob_name(run_id: str, source_name: str, filename: str) -> str:
    return f"{run_id}/{source_name}/{filename}"


def _basename_from_url(url: str, source_name: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or f"{source_name}.bin"
    return name


def _upload_bytes_to_transient(
    blob,
    *,
    container_name: str,
    blob_name: str,
    local_path: str,
) -> None:
    """Upload local_path bytes to {container_name}/{blob_name}."""
    if blob is None:
        raise RuntimeError("source_fetch_engine: blob client is required")
    container = blob.get_container_client(container_name)
    try:
        container.create_container()
    except Exception:
        pass
    blob_client = container.get_blob_client(blob_name)
    with open(local_path, "rb") as fh:
        blob_client.upload_blob(fh, overwrite=True)


def _download_source(
    *,
    source_name: str,
    spec: dict,
    gate: RateLimitedGate,
    http_timeout: int,
) -> tuple[str, str, int]:
    """Download to a tmp file, return (local_path, sha256, size_bytes)."""
    url = _resolve_source_url(source_name, spec)
    gate.acquire()
    _log.info("source_fetch_engine[%s]: GET %s", source_name, url)
    resp = requests.get(url, stream=True, timeout=http_timeout)
    resp.raise_for_status()
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=f"{source_name}_", suffix=".bin")
    hasher = hashlib.sha256()
    size = 0
    try:
        for chunk in resp.iter_content(chunk_size=DEFAULT_CHUNK_BYTES):
            if not chunk:
                continue
            tmp.write(chunk)
            hasher.update(chunk)
            size += len(chunk)
    finally:
        tmp.close()
    return tmp.name, hasher.hexdigest(), size


def _fetch_one_source(
    *,
    source_name: str,
    spec: dict,
    run_id: str,
    env_prefix: str,
    freshness_decision: str,
    blob,
    transient_container: str,
    gate: RateLimitedGate,
    http_timeout: int,
) -> dict[str, Any]:
    """Fetch one source; return the per-source result record."""
    result: dict[str, Any] = {
        "source_name": source_name,
        "run_id": run_id,
        "env": env_prefix,
        "started_at": _now_iso(),
        "freshness_decision": freshness_decision,
    }
    if freshness_decision == "reuse":
        result["skipped"] = True
        result["reason"] = "freshness_reuse"
        result["finished_at"] = _now_iso()
        return result

    try:
        local_path, sha256, size = _download_source(
            source_name=source_name,
            spec=spec,
            gate=gate,
            http_timeout=http_timeout,
        )
    except Exception as exc:
        result["skipped"] = True
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["finished_at"] = _now_iso()
        raise

    try:
        filename = spec.get("filename") or _basename_from_url(
            _resolve_source_url(source_name, spec), source_name
        )
        blob_name = _target_blob_name(run_id, source_name, filename)
        _upload_bytes_to_transient(
            blob,
            container_name=transient_container,
            blob_name=blob_name,
            local_path=local_path,
        )
    finally:
        try:
            os.unlink(local_path)
        except OSError:
            pass

    result["blob_container"] = transient_container
    result["blob_path"] = blob_name
    result["filename"] = filename
    result["sha256"] = sha256
    result["size_bytes"] = size
    result["version"] = sha256[:16]
    result["skipped"] = False
    result["finished_at"] = _now_iso()
    return result


def _build_gate(source_name: str, throttle_rates: dict) -> RateLimitedGate:
    rate = float(throttle_rates.get(source_name, throttle_rates.get("default", 4.0)))
    return RateLimitedGate(rate_per_second=rate)


def _persist_versions_to_manifest(
    mongo, *, run_id: str, source_versions: dict[str, str]
) -> None:
    """Write source_versions block to chathealthyfrontend.pipeline.runs (LLD §3.6)."""
    if mongo is None:
        return
    try:
        coll = mongo["chathealthyfrontend"]["pipeline.runs"]
        coll.update_one(
            {"run_id": run_id},
            {"$set": {"source_versions": source_versions, "updated_at": _now_iso()}},
        )
    except Exception as exc:
        _log.warning("source_fetch_engine: manifest update failed: %s", exc)


def fetch_all_sources(
    config: dict,
    *,
    mongo=None,
    blob=None,
) -> dict[str, Any]:
    """Fetch every source in `config["sources"]` in parallel.

    Required config keys:
      - sources: dict[source_name -> spec]
          spec fields:
            source_url         (str, optional if url_discovery set)
            url_discovery      (dict with page_url, instructions)
            filename           (str, optional; defaults to URL basename)
            freshness_decision (str, "reuse" or "fetch")
      - run_id                (str)
      - env                   (str, "local"|"dev"|"qa"|"prod")
      - transient_container   (str, defaults to "{env}-pipeline-transients")
      - fetch_concurrency     (int, defaults to 6)
      - http_timeout_seconds  (int, defaults to 600)
      - throttle_rates        (dict, source_name -> req/s; "default" fallback)

    Returns:
      {
        "results":         list of per-source result records,
        "source_versions": {source_name: version} for fetched sources,
        "fetched_count":   int,
        "skipped_count":   int,
        "errors":          list of {source_name, error} entries,
      }
    """
    sources = config.get("sources") or {}
    if not sources:
        raise RuntimeError("source_fetch_engine: config['sources'] is empty")

    run_id = config.get("run_id")
    if not run_id:
        raise RuntimeError("source_fetch_engine: config['run_id'] is required")

    env_prefix = config.get("env") or "dev"
    transient_container = config.get(
        "transient_container", f"{env_prefix}-pipeline-transients"
    )
    concurrency = int(config.get("fetch_concurrency", DEFAULT_FETCH_CONCURRENCY))
    http_timeout = int(config.get("http_timeout_seconds", DEFAULT_HTTP_TIMEOUT_SEC))
    throttle_rates = config.get("throttle_rates") or {}

    per_source_gates: dict[str, RateLimitedGate] = {
        name: _build_gate(name, throttle_rates) for name in sources.keys()
    }

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        future_to_name: dict[Any, str] = {}
        for name, spec in sources.items():
            decision = spec.get("freshness_decision", "fetch")
            fut = pool.submit(
                _fetch_one_source,
                source_name=name,
                spec=spec,
                run_id=run_id,
                env_prefix=env_prefix,
                freshness_decision=decision,
                blob=blob,
                transient_container=transient_container,
                gate=per_source_gates[name],
                http_timeout=http_timeout,
            )
            future_to_name[fut] = name

        for fut in as_completed(future_to_name):
            name = future_to_name[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                errors.append({"source_name": name, "error": f"{type(exc).__name__}: {exc}"})
                _log.exception("source_fetch_engine[%s]: fetch failed", name)

    source_versions = {
        r["source_name"]: r["version"] for r in results if not r.get("skipped") and r.get("version")
    }
    fetched = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))

    _persist_versions_to_manifest(
        mongo, run_id=run_id, source_versions=source_versions
    )

    return {
        "results": results,
        "source_versions": source_versions,
        "fetched_count": fetched,
        "skipped_count": skipped,
        "errors": errors,
    }
