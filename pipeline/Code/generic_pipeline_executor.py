# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""GenericPipelineExecutor: registry driven, three phase pipeline runner.

The executor lives entirely on the PIPELINE cluster. Coordination metadata
(loaded_metadata rows keyed by source_name) is written to
`Pipelines.pipeline.loaded_metadata` on the metadata connection
connection. The executor never touches the frontend cluster.

Phase 1 (fetch_phase):
  For every fetched entry, resolve the concrete URL, sha256 the payload,
  compare the digest to the fetcher's last loaded bundle_hash on
  pipeline.loaded_metadata. Unchanged means the fetcher and every bundle
  sibling stay fresh; changed means the archive is downloaded, every
  bundle member is extracted to an individually addressable blob, and
  every affected entry is marked stale.

Phase 2 (freshness_cascade_phase):
  Walk the registry in topological order. Every Class C entry is stale
  iff any entry in its transitive dependency graph is stale; otherwise
  fresh. Fetched and bundled entries pass through untouched (their
  freshness is written by phase 1).

Phase 3 (stage_and_publish_phase):
  For every entry with a public_data_name AND freshness=stale, cross-DB
  rename staging -> public_data via admin.command('renameCollection',
  ..., dropTarget=True); mark fresh, update source_hash and loaded_at.

Every deliberate raise inside the executor first records the fatal to
pipeline.discrepancies via pipeline_fatal_recorder.record_fatal_
discrepancy so the discrepancy report step sees the failure.
"""

from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import tempfile
import zipfile
from datetime import datetime, timezone
from typing import Optional

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline_dataset_registry import DatasetEntry, PipelineDatasetRegistry
from pipeline_fatal_recorder import record_fatal_discrepancy

_log = ChatHealthyLoggingService()

# Pipeline cluster coordination DB (per operator directive 2026-08-03).
from pipeline_db import PIPELINE_ADMIN_DB
_CHUNK_BYTES = 1024 * 1024
_TRANSIENT_CONTAINER = "pipeline-transients"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(_CHUNK_BYTES)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def _upload_bytes(blob_client, container: str, blob_path: str, payload: bytes) -> None:
    if blob_client is None:
        return
    put = getattr(blob_client, "put_blob", None)
    if callable(put):
        put(container, blob_path, payload)
        return
    cc = blob_client.get_container_client(container)
    try:
        cc.create_container()
    except Exception:  # noqa: BLE001
        pass
    cc.get_blob_client(blob_path).upload_blob(io.BytesIO(payload), overwrite=True)


class GenericPipelineExecutor:
    """Registry driven, three phase pipeline runner. Each phase is invoked
    independently by the orchestrator."""

    _STEP = "generic_pipeline_executor"

    def __init__(
        self,
        registry: PipelineDatasetRegistry,
        pipeline_mongo,
        blob_client,
        run_id: str,
    ) -> None:
        self._pipeline_mongo = pipeline_mongo
        self._run_id: Optional[str] = run_id if isinstance(run_id, str) and run_id else None
        if not isinstance(registry, PipelineDatasetRegistry):
            self._fatal(ChatHealthyException(
                mode="pipeline_executor_registry_wrong_type",
                message=(
                    "generic_pipeline_executor: registry must be a "
                    f"PipelineDatasetRegistry; got {type(registry).__name__}."
                ),
            ))
        if pipeline_mongo is None:
            self._fatal(ChatHealthyException(
                mode="pipeline_executor_pipeline_mongo_missing",
                message="generic_pipeline_executor: pipeline_mongo is required.",
            ))
        if not isinstance(run_id, str) or not run_id:
            self._fatal(ChatHealthyException(
                mode="pipeline_executor_run_id_missing",
                message="generic_pipeline_executor: run_id must be a non-empty string.",
            ))
        self._registry = registry
        self._blob_client = blob_client
        self._transient_container = _TRANSIENT_CONTAINER

    # ------------------------------------------------------------------
    # fatal helpers
    # ------------------------------------------------------------------

    def _fatal(self, exc: ChatHealthyException):
        record_fatal_discrepancy(
            self._pipeline_mongo, run_id=self._run_id, step=self._STEP, exc=exc,
        )
        raise exc

    # ------------------------------------------------------------------
    # metadata helpers (admin target: the Pipelines DB)
    # ------------------------------------------------------------------

    def _metadata_coll(self):
        return self._pipeline_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]

    def _read_metadata(self, source_name: str) -> Optional[dict]:
        return self._metadata_coll().find_one({"_id": source_name})

    def _write_freshness(
        self,
        source_name: str,
        *,
        freshness: str,
        kind: str,
        bundle_hash: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> None:
        doc: dict = {
            "source_name": source_name,
            "freshness": freshness,
            "loaded_at": _now_iso(),
            "run_id": self._run_id,
            "kind": kind,
        }
        if bundle_hash is not None:
            doc["bundle_hash"] = bundle_hash
            doc["source_hash"] = bundle_hash
        if extra:
            doc.update(extra)
        self._metadata_coll().update_one(
            {"_id": source_name}, {"$set": doc}, upsert=True
        )

    # ------------------------------------------------------------------
    # download plumbing
    # ------------------------------------------------------------------

    def _download_to_tmp(self, url: str, source_name: str) -> tuple[str, str, int]:
        """Download url to a tmp file. Returns (tmp_path, sha256, size).

        A blob_client with a callable http_get(url) -> bytes attribute is
        used when present (tests inject a fake); otherwise the runtime
        falls back to requests.get for real HTTP."""
        fetcher = getattr(self._blob_client, "http_get", None) if self._blob_client is not None else None
        if callable(fetcher):
            body = fetcher(url)
            if not isinstance(body, (bytes, bytearray)):
                self._fatal(ChatHealthyException(
                    mode="pipeline_executor_http_get_wrong_return_type",
                    message=(
                        f"generic_pipeline_executor[{source_name}]: injected "
                        f"blob_client.http_get MUST return bytes; got "
                        f"{type(body).__name__}."
                    ),
                    source_name=source_name,
                ))
            payload = bytes(body)
            fd, path = tempfile.mkstemp(prefix=f"{source_name}_", suffix=".bin")
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
            return path, _sha256_bytes(payload), len(payload)
        # Real download path.
        import requests  # noqa: PLC0415 - lazy so tests never need requests installed
        resp = requests.get(url, stream=True, timeout=600)
        resp.raise_for_status()
        fd, path = tempfile.mkstemp(prefix=f"{source_name}_", suffix=".bin")
        hasher = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=_CHUNK_BYTES):
                if not chunk:
                    continue
                fh.write(chunk)
                hasher.update(chunk)
                size += len(chunk)
        return path, hasher.hexdigest(), size

    def _extract_and_upload_members(
        self,
        archive_path: str,
        fetcher_entry: DatasetEntry,
        bundle_members: list[DatasetEntry],
    ) -> dict[str, str]:
        """Open the archive, locate every member's archive_member glob, and
        upload each member individually into the transient container at
        deterministic blob paths. Returns {source_name: blob_path}."""
        mapping: dict[str, str] = {}
        with zipfile.ZipFile(archive_path) as zf:
            names = zf.namelist()
            for member in bundle_members:
                glob = member.archive_member
                if not glob:
                    self._fatal(ChatHealthyException(
                        mode="pipeline_executor_bundle_member_missing_glob",
                        message=(
                            f"generic_pipeline_executor: bundle member "
                            f"{member.source_name!r} has no archive_member "
                            f"glob; every bundled_with entry MUST declare one."
                        ),
                        source_name=member.source_name,
                    ))
                matched = [n for n in names if fnmatch.fnmatch(n.lower(), glob.lower())]
                if not matched:
                    self._fatal(ChatHealthyException(
                        mode="pipeline_executor_archive_member_not_found",
                        message=(
                            f"generic_pipeline_executor[{member.source_name}]: "
                            f"no member in {fetcher_entry.source_name!r} archive "
                            f"matches glob {glob!r}. Archive members: {names[:10]}..."
                        ),
                        source_name=member.source_name,
                    ))
                entry_name = matched[0]
                payload = zf.read(entry_name)
                blob_path = f"{self._run_id}/{member.source_name}/{os.path.basename(entry_name)}"
                _upload_bytes(self._blob_client, self._transient_container, blob_path, payload)
                mapping[member.source_name] = blob_path
        return mapping

    # ------------------------------------------------------------------
    # phase 1
    # ------------------------------------------------------------------

    def fetch_phase(self) -> dict[str, str]:
        summary: dict[str, str] = {}
        for entry in self._registry.entries():
            if not entry.is_fetched:
                continue
            self._fetch_one_fetcher(entry, summary)
        return summary

    def _fetch_one_fetcher(self, entry: DatasetEntry, summary: dict[str, str]) -> None:
        members: list[DatasetEntry] = [entry]
        if entry.is_bundled:
            members = self._registry.bundle_members(entry.bundled_with)
        url = self._registry.resolve_source_url(entry.source_name)
        prior = self._read_metadata(entry.source_name) or {}
        prior_hash = (prior.get("bundle_hash") or "").strip()
        tmp_path, downloaded_hash, _size = self._download_to_tmp(url, entry.source_name)
        try:
            unchanged = bool(prior_hash) and prior_hash == downloaded_hash
            verdict = "fresh" if unchanged else "stale"
            if not unchanged:
                if entry.is_bundled:
                    self._extract_and_upload_members(tmp_path, entry, members)
                else:
                    with open(tmp_path, "rb") as fh:
                        payload = fh.read()
                    _upload_bytes(
                        self._blob_client,
                        self._transient_container,
                        f"{self._run_id}/{entry.source_name}/{os.path.basename(url)}",
                        payload,
                    )
            for member in members:
                kind = "bundled" if member.is_bundled else "fetched"
                self._write_freshness(
                    member.source_name,
                    freshness=verdict,
                    kind=kind,
                    bundle_hash=downloaded_hash if member.source_name == entry.source_name else None,
                    extra={
                        "public_data_collection": self._registry.public_data_collection_name(member.source_name),
                    },
                )
                summary[member.source_name] = verdict
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # phase 2
    # ------------------------------------------------------------------

    def freshness_cascade_phase(self) -> dict[str, str]:
        summary: dict[str, str] = {}
        for entry in self._registry.topological_order():
            if not entry.is_derived:
                meta = self._read_metadata(entry.source_name) or {}
                summary[entry.source_name] = (meta.get("freshness") or "stale")
                continue
            verdict = "fresh"
            for dep in self._registry.dependencies_of(entry.source_name):
                dep_verdict = summary.get(dep.source_name)
                if dep_verdict is None:
                    dep_meta = self._read_metadata(dep.source_name) or {}
                    dep_verdict = dep_meta.get("freshness") or "stale"
                    summary[dep.source_name] = dep_verdict
                if dep_verdict == "stale":
                    verdict = "stale"
                    break
            summary[entry.source_name] = verdict
            self._write_freshness(
                entry.source_name,
                freshness=verdict,
                kind="derived",
                extra={
                    "public_data_collection": self._registry.public_data_collection_name(entry.source_name),
                    "depends_on": list(entry.depends_on or ()),
                },
            )
        return summary

    # ------------------------------------------------------------------
    # phase 3
    # ------------------------------------------------------------------

    def stage_and_publish_phase(self) -> dict[str, dict]:
        report: dict[str, dict] = {}
        for entry in self._registry.entries():
            meta = self._read_metadata(entry.source_name) or {}
            freshness = meta.get("freshness")
            if freshness == "fresh":
                report[entry.source_name] = {"published": False, "reason": "already fresh"}
                continue
            if freshness != "stale":
                report[entry.source_name] = {
                    "published": False,
                    "reason": f"no freshness verdict; got {freshness!r}",
                }
                continue
            staging_name = self._registry.staging_collection_name(entry.source_name)
            public_name = self._registry.public_data_collection_name(entry.source_name)
            staging_db = self._pipeline_mongo[entry.staging_db]
            if staging_name not in staging_db.list_collection_names():
                report[entry.source_name] = {
                    "published": False,
                    "reason": (
                        f"staging collection {entry.staging_db}.{staging_name} "
                        f"is missing on pipeline cluster"
                    ),
                }
                continue
            self._pipeline_mongo.admin.command({
                "renameCollection": f"{entry.staging_db}.{staging_name}",
                "to": f"{entry.public_data_db}.{public_name}",
                "dropTarget": True,
            })
            row_count = self._pipeline_mongo[entry.public_data_db][public_name].count_documents({})
            bundle_hash = meta.get("bundle_hash")
            self._write_freshness(
                entry.source_name,
                freshness="fresh",
                kind=meta.get("kind") or ("derived" if entry.is_derived else "fetched"),
                bundle_hash=bundle_hash,
                extra={
                    "public_data_collection": public_name,
                    "row_count": row_count,
                    "published_at": _now_iso(),
                },
            )
            report[entry.source_name] = {
                "published": True,
                "public_data_collection": public_name,
                "row_count": row_count,
            }
        return report
