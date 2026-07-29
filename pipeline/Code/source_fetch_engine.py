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
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import fnmatch
import hashlib

import os
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from source_url_discovery import find_latest_data_url
from throttle_semaphore import RateLimitedGate

_log = ChatHealthyLoggingService()

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
        raise ChatHealthyException(mode="runtime_error", message=f"source_fetch_engine[{source_name}]: url_discovery.page_url is required")
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
        raise ChatHealthyException(mode="runtime_error", message=f"source_fetch_engine[{source_name}]: no source_url or url_discovery configured")
    return url


def _target_blob_name(run_id: str, source_name: str, filename: str) -> str:
    return f"{run_id}/{source_name}/{filename}"


def _basename_from_url(url: str, source_name: str) -> str:
    parsed = urlparse(url)
    name = os.path.basename(parsed.path) or f"{source_name}.bin"
    return name


def _require_blob_client(blob) -> None:
    if blob is None:
        raise ChatHealthyException(mode="runtime_error", message="source_fetch_engine: blob client is required")


def _upload_bytes_to_transient(
    blob,
    *,
    container_name: str,
    blob_name: str,
    local_path: str,
) -> None:
    """Upload local_path bytes to {container_name}/{blob_name}."""
    import time as _t  # noqa: PLC0415
    _require_blob_client(blob)
    local_size = os.path.getsize(local_path)
    _log.info(
        "source_fetch_engine.upload: START container=%s blob=%s local_size=%d bytes (%.2f MB)",
        container_name, blob_name, local_size, local_size / 1024 / 1024,
    )
    container = blob.get_container_client(container_name)
    t_create = _t.time()
    try:
        container.create_container()
        _log.info(
            "source_fetch_engine.upload: container.create_container OK container=%s elapsed=%.2fs",
            container_name, _t.time() - t_create,
        )
    except Exception as _cc_exc:  # noqa: BLE001
        _log.info(
            "source_fetch_engine.upload: container.create_container skipped container=%s reason=%s elapsed=%.2fs",
            container_name, type(_cc_exc).__name__, _t.time() - t_create,
        )
    blob_client = container.get_blob_client(blob_name)
    t_up = _t.time()
    with open(local_path, "rb") as fh:
        blob_client.upload_blob(fh, overwrite=True)
    elapsed = _t.time() - t_up
    mbps = (local_size / 1024 / 1024) / elapsed if elapsed > 0 else 0
    _log.info(
        "source_fetch_engine.upload: DONE container=%s blob=%s size=%d bytes elapsed=%.2fs avg=%.2f MB/s",
        container_name, blob_name, local_size, elapsed, mbps,
    )


def _download_source(
    *,
    source_name: str,
    spec: dict,
    gate: RateLimitedGate,
    http_timeout: int,
) -> tuple[str, str, int]:
    """Download to a tmp file, return (local_path, sha256, size_bytes)."""
    import time as _t  # noqa: PLC0415
    url = _resolve_source_url(source_name, spec)
    gate.acquire()
    _log.info("source_fetch_engine[%s]: GET %s (http_timeout=%ds)", source_name, url, http_timeout)
    t0 = _t.time()
    # ChatHealthy-Pipeline UA + optional pipeline auth header. Cloudflare
    # bot protection on chathealthy.ai/* rejects the default python-requests
    # UA; the auth header (when set) matches a Cloudflare custom rule that
    # bypasses BIC/WAF for the /Data/* pipeline fetch path. Other origins
    # (NPPES, NUCC, Census, USDA) ignore both headers.
    headers = {"User-Agent": "ChatHealthy-Pipeline/1.0"}
    auth_hdr = os.environ.get("CLOUDFLARE_PIPELINE_AUTH_HEADER", "").strip()
    if auth_hdr:
        headers["X-ChatHealthy-Pipeline-Auth"] = auth_hdr
    resp = requests.get(url, stream=True, timeout=http_timeout, headers=headers)
    resp.raise_for_status()
    content_len = resp.headers.get("Content-Length", "?")
    _log.info(
        "source_fetch_engine[%s]: connected status=%d content-length=%s",
        source_name, resp.status_code, content_len,
    )
    tmp = tempfile.NamedTemporaryFile(delete=False, prefix=f"{source_name}_", suffix=".bin")
    hasher = hashlib.sha256()
    size = 0
    last_report_mb = 0
    try:
        for chunk in resp.iter_content(chunk_size=DEFAULT_CHUNK_BYTES):
            if not chunk:
                continue
            tmp.write(chunk)
            hasher.update(chunk)
            size += len(chunk)
            # Progress log every 100 MB. Cheap; small files (nucc,
            # census, usda) never cross 100 MB so they produce zero
            # extra lines; NPPES (~5 GB) produces ~50 lines total.
            mb = size / 1024 / 1024
            if int(mb) >= last_report_mb + 100:
                elapsed = _t.time() - t0
                mbps = mb / elapsed if elapsed > 0 else 0
                _log.info(
                    "source_fetch_engine[%s]: progress %.1f MB elapsed=%.1fs avg=%.2f MB/s",
                    source_name, mb, elapsed, mbps,
                )
                last_report_mb = int(mb)
    finally:
        tmp.close()
    elapsed = _t.time() - t0
    mbps = (size / 1024 / 1024) / elapsed if elapsed > 0 else 0
    _log.info(
        "source_fetch_engine[%s]: download DONE size=%d bytes (%.2f MB) sha256=%s elapsed=%.2fs avg=%.2f MB/s local=%s",
        source_name, size, size / 1024 / 1024, hasher.hexdigest()[:16], elapsed, mbps, tmp.name,
    )
    return tmp.name, hasher.hexdigest(), size


def _require_derive_inputs(source_name: str, parent_container, parent_blob_name, zip_glob) -> None:
    if not (parent_container and parent_blob_name and zip_glob):
        raise ChatHealthyException(mode="runtime_error", message=f"source_fetch_engine[{source_name}]: derived_from requires parent blob + zip_entry_glob")


def _require_blob_client_for_derive(source_name: str, blob) -> None:
    if blob is None:
        raise ChatHealthyException(mode="runtime_error", message=f"source_fetch_engine[{source_name}]: blob client is required for derived_from")


def _resolve_zip_entry(source_name: str, zip_glob: str, parent_blob_name: str, zf) -> str:
    glob_lower = zip_glob.lower()
    for n in zf.namelist():
        if fnmatch.fnmatch(n.lower(), glob_lower):
            return n
    raise ChatHealthyException(
        mode="runtime_error",
        message=f"source_fetch_engine[{source_name}]: no entry matching {zip_glob!r} in {parent_blob_name}",
        names_head=zf.namelist()[:10],
    )


def _extract_derived_source(
    *,
    source_name: str,
    spec: dict,
    parent_result: dict,
    blob,
) -> tuple[str, str, int, str]:
    """Extract a derived source (zip entry) from a parent source's blob.

    Returns (local_path, sha256, size_bytes, entry_name).
    Downloads parent zip from blob to a tmp file, opens with zipfile,
    finds the first entry matching zip_entry_glob (case-insensitive),
    streams it to a tmp file with sha256.
    """
    import time as _t  # noqa: PLC0415
    parent_container = parent_result.get("blob_container")
    parent_blob_name = parent_result.get("blob_path")
    zip_glob = spec.get("zip_entry_glob")
    _require_derive_inputs(source_name, parent_container, parent_blob_name, zip_glob)
    _require_blob_client_for_derive(source_name, blob)
    _log.info(
        "source_fetch_engine.derive[%s]: START parent_container=%s parent_blob=%s zip_glob=%s",
        source_name, parent_container, parent_blob_name, zip_glob,
    )
    container_client = blob.get_container_client(parent_container)
    parent_client = container_client.get_blob_client(parent_blob_name)
    fd, parent_tmp = tempfile.mkstemp(suffix=".zip", prefix=f"{source_name}_parent_")
    os.close(fd)
    try:
        t_dl = _t.time()
        parent_bytes = 0
        with open(parent_tmp, "wb") as fh:
            stream = parent_client.download_blob(max_concurrency=4)
            for chunk in stream.chunks():
                fh.write(chunk)
                parent_bytes += len(chunk)
        _log.info(
            "source_fetch_engine.derive[%s]: parent blob downloaded size=%.2f MB elapsed=%.2fs",
            source_name, parent_bytes / 1024 / 1024, _t.time() - t_dl,
        )
        with zipfile.ZipFile(parent_tmp) as zf:
            entry_name = _resolve_zip_entry(source_name, zip_glob, parent_blob_name, zf)
            _log.info(
                "source_fetch_engine.derive[%s]: extracting entry=%s from parent zip",
                source_name, entry_name,
            )
            t_ex = _t.time()
            out = tempfile.NamedTemporaryFile(
                delete=False, prefix=f"{source_name}_", suffix=".bin",
            )
            hasher = hashlib.sha256()
            size = 0
            try:
                with zf.open(entry_name) as raw:
                    while True:
                        buf = raw.read(DEFAULT_CHUNK_BYTES)
                        if not buf:
                            break
                        out.write(buf)
                        hasher.update(buf)
                        size += len(buf)
            finally:
                out.close()
            _log.info(
                "source_fetch_engine.derive[%s]: extract DONE size=%.2f MB sha256=%s elapsed=%.2fs local=%s",
                source_name, size / 1024 / 1024, hasher.hexdigest()[:16], _t.time() - t_ex, out.name,
            )
            return out.name, hasher.hexdigest(), size, os.path.basename(entry_name)
    finally:
        try:
            os.unlink(parent_tmp)
        except OSError:
            pass


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
        # Reuse the archived blob from the LAST successful fetch.
        # source_freshness_gate has verified the source's version
        # identifier is unchanged AND the archive blob exists; the gate
        # threaded the archive coords into spec so downstream steps see
        # a normal fetch_result (blob_container + blob_path) and do not
        # know the difference between a fresh download and a reuse.
        archive_container = spec.get("archive_container")
        archive_blob = spec.get("archive_blob")
        archive_version = spec.get("archived_version")
        archive_filename = spec.get("archive_filename")
        if not (archive_container and archive_blob):
            raise ChatHealthyException(
                mode="runtime_error",
                message=(
                    f"_fetch_one_source[{source_name}]: freshness_decision=reuse "
                    f"but spec is missing archive_container/archive_blob — "
                    f"source_freshness_gate must attach these when deciding reuse"
                ),
                component="source_fetch_engine",
                source_name=source_name,
            )
        result["blob_container"] = archive_container
        result["blob_path"] = archive_blob
        result["filename"] = archive_filename or archive_blob.rsplit("/", 1)[-1]
        result["source_version_identifier"] = archive_version or ""
        result["skipped"] = False
        result["reused_from_archive"] = True
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
    # source-side identifier used by the NEXT run's freshness_gate to
    # decide reuse. Probe once now against the URL we just fetched so
    # the identifier reflects the version we actually archived. Silent
    # None on probe failure: freshness reuse degrades to always-fetch
    # next run, but the current fetch/archive succeeds. Rule-005 keeps
    # the log call in the caller (fetch_all_sources), not in this
    # raise-capable function body.
    try:
        from source_freshness_probe import probe_source_version
        result["source_version_identifier"] = probe_source_version(
            source_name, _resolve_source_url(source_name, spec),
        )
    except Exception:  # noqa: BLE001
        result["source_version_identifier"] = None
        result["source_version_probe_failed"] = True
    result["skipped"] = False
    result["finished_at"] = _now_iso()
    return result


def _derive_one_source(
    *,
    source_name: str,
    spec: dict,
    parent_result: dict,
    run_id: str,
    env_prefix: str,
    blob,
    transient_container: str,
) -> dict[str, Any]:
    """Materialize one derived source by extracting from its parent's blob."""
    result: dict[str, Any] = {
        "source_name": source_name,
        "run_id": run_id,
        "env": env_prefix,
        "started_at": _now_iso(),
        "freshness_decision": spec.get("freshness_decision", "fetch"),
        "derived_from": spec.get("derived_from"),
    }
    if spec.get("freshness_decision") == "reuse":
        result["skipped"] = True
        result["reason"] = "freshness_reuse"
        result["finished_at"] = _now_iso()
        return result

    local_path, sha256, size, entry_name = _extract_derived_source(
        source_name=source_name,
        spec=spec,
        parent_result=parent_result,
        blob=blob,
    )
    try:
        filename = spec.get("filename") or entry_name
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


def _purge_prior_run_transients(blob, container_name: str, current_run_id: str) -> int:
    """Delete every blob in the transient container that does NOT belong
    to the current run_id. Design intent per operator: when a new run
    lands, its blobs replace prior runs' blobs — the transient container
    holds at most one run's worth of intermediates at any moment. Returns
    the count deleted (best effort — errors log-warn, do not raise)."""
    if blob is None:
        return 0
    try:
        cc = blob.get_container_client(container_name)
        purged = 0
        for b in cc.list_blobs():
            # Blob names begin with the run_id prefix (see _target_blob_name).
            # Anything not starting with the current run_id is a prior-run leftover.
            if not b.name.startswith(f"{current_run_id}/"):
                try:
                    cc.delete_blob(b.name)
                    purged += 1
                except Exception as exc:  # noqa: BLE001
                    _log.warning(
                        "source_fetch_engine: purge failed for %s: %s",
                        b.name, exc,
                    )
        if purged:
            _log.info(
                "source_fetch_engine: purged %d prior-run blob(s) from %s (kept only run_id=%s)",
                purged, container_name, current_run_id,
            )
        return purged
    except Exception as exc:  # noqa: BLE001
        _log.warning(
            "source_fetch_engine: prior-run purge failed on container %s: %s",
            container_name, exc,
        )
        return 0


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
        raise ChatHealthyException(mode="runtime_error", message="source_fetch_engine: config['sources'] is empty")

    run_id = config.get("run_id")
    if not run_id:
        raise ChatHealthyException(mode="runtime_error", message="source_fetch_engine: config['run_id'] is required")

    env_prefix = config.get("env") or "dev"
    transient_container = config.get(
        "transient_container", f"{env_prefix}-pipeline-transients"
    )
    concurrency = int(config.get("fetch_concurrency", DEFAULT_FETCH_CONCURRENCY))
    http_timeout = int(config.get("http_timeout_seconds", DEFAULT_HTTP_TIMEOUT_SEC))
    throttle_rates = config.get("throttle_rates") or {}

    base_sources = {n: s for n, s in sources.items() if not s.get("derived_from")}
    derived_sources = {n: s for n, s in sources.items() if s.get("derived_from")}

    _purge_prior_run_transients(blob, transient_container, run_id)

    per_source_gates: dict[str, RateLimitedGate] = {
        name: _build_gate(name, throttle_rates) for name in base_sources.keys()
    }

    results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    results_by_name: dict[str, dict] = {}

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        future_to_name: dict[Any, str] = {}
        for name, spec in base_sources.items():
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
                r = fut.result()
                results.append(r)
                results_by_name[name] = r
            except Exception as exc:
                errors.append({"source_name": name, "error": f"{type(exc).__name__}: {exc}"})

    for name, spec in derived_sources.items():
        parent = spec.get("derived_from")
        parent_result = results_by_name.get(parent)
        if not parent_result or parent_result.get("skipped") or not parent_result.get("blob_path"):
            errors.append({
                "source_name": name,
                "error": f"derived_from parent {parent!r} unavailable",
            })
            continue
        try:
            r = _derive_one_source(
                source_name=name,
                spec=spec,
                parent_result=parent_result,
                run_id=run_id,
                env_prefix=env_prefix,
                blob=blob,
                transient_container=transient_container,
            )
            results.append(r)
            results_by_name[name] = r
        except Exception as exc:
            errors.append({"source_name": name, "error": f"{type(exc).__name__}: {exc}"})

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
