# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import csv
import io
import logging
import zipfile

from pymongo import InsertOne

_log = logging.getLogger("staging_collections")


def load_staging_collection(db, coll_name: str, docs, *, incremental: bool = False) -> dict:
    coll = db[coll_name]
    if incremental:
        upserted = 0
        for doc in docs:
            key = doc.get("_id") or doc.get("npi") or doc.get("source_key")
            if key is None:
                coll.insert_one(doc)
                upserted += 1
            else:
                coll.update_one({"_id": key} if "_id" in doc else {"npi": doc.get("npi")}, {"$set": doc}, upsert=True)
                upserted += 1
        return {"mode": "incremental", "upserted": upserted}
    coll.drop()
    if docs:
        coll.insert_many(list(docs), ordered=False)
    return {"mode": "full", "inserted": len(docs)}


def _chunked_blob_reader(downloader):
    """Binary reader over Azure blob chunks for TextIOWrapper/csv."""

    class _Reader(io.RawIOBase):
        def __init__(self):
            self._iter = iter(downloader.chunks())
            self._buf = b""
            self._done = False

        def readable(self):
            return True

        def read(self, size=-1):
            if size == -1:
                parts = [self._buf, *self._iter]
                self._buf = b""
                self._done = True
                return b"".join(parts)
            while len(self._buf) < size and not self._done:
                try:
                    self._buf += next(self._iter)
                except StopIteration:
                    self._done = True
                    break
            out = self._buf[:size]
            self._buf = self._buf[size:]
            return out

        def readinto(self, b):
            data = self.read(len(b))
            n = len(data)
            b[:n] = data
            return n

    return _Reader()


def _stream_csv_rows(blob_client, *, start_offset: int = 0):
    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    header_line = header_text.split("\n")[0]
    header = list(csv.reader([header_line]))[0]
    header_end = len(header_line.encode("utf-8")) + 1
    downloader = blob_client.download_blob(offset=max(start_offset, header_end))
    text = io.TextIOWrapper(
        io.BufferedReader(_chunked_blob_reader(downloader)),
        encoding="utf-8",
        errors="replace",
        newline="",
    )
    reader = csv.reader(text)
    for row in reader:
        if len(row) != len(header):
            continue
        raw = {h: v for h, v in zip(header, row) if (v or "").strip()}
        if raw:
            yield raw



def _load_nppes_staging(ctx, rt, states: list[str]) -> dict:
    from blob_client import get_blob_service
    from provider_load_manager import download_zip_fn, extract_csv_fn
    from provider_worker import _raw_row_matches_state
    from state_filter import is_full_load

    container = ctx.config.get("staging_container", "provider-data")
    dl_cfg = {"blob_container": container, "env_prefix": ctx.env_prefix, "states": states}
    download = download_zip_fn(dl_cfg)
    csv_path = extract_csv_fn({**dl_cfg, **download})

    coll = rt.staging_coll("nppes_npi")
    if not ctx.args.incremental:
        coll.delete_many({"run_id": rt.run_id})

    service = get_blob_service()
    blob = service.get_container_client(container).get_blob_client(csv_path)
    full_load = is_full_load(states)
    batch: list[InsertOne] = []
    inserted = skipped = 0
    seen_npis: set[str] = set()
    batch_size = int(ctx.config.get("batch_limits", {}).get("normalize_batch_size", 1000))

    for raw in _stream_csv_rows(blob):
        if not full_load and not _raw_row_matches_state(raw, states):
            skipped += 1
            continue
        npi = (raw.get("NPI") or "").strip()
        if not npi:
            continue
        if npi in seen_npis:
            continue
        seen_npis.add(npi)
        batch.append(InsertOne({"npi": npi, "run_id": rt.run_id, "raw": raw}))
        if len(batch) >= batch_size:
            coll.bulk_write(batch, ordered=False)
            inserted += len(batch)
            batch = []

    if batch:
        coll.bulk_write(batch, ordered=False)
        inserted += len(batch)

    return {"source": "nppes_npi", "inserted": inserted, "skipped_states": skipped, "csv_path": csv_path}


def _load_pl_pfile_staging(ctx, rt, states: list[str]) -> dict:
    from blob_client import get_blob_service
    from provider_load_manager import download_zip_fn
    from provider_worker import _raw_row_matches_state
    from state_filter import is_full_load

    nppes_container = ctx.args.blob_container or "provider-data"
    dl_cfg = {"blob_container": nppes_container, "env_prefix": ctx.env_prefix, "states": states}
    download = download_zip_fn(dl_cfg)
    zip_blob_name = download["zip_path"]

    coll = rt.staging_coll("pl_pfile")
    if not ctx.args.incremental:
        coll.delete_many({"run_id": rt.run_id})

    import os
    import tempfile

    service = get_blob_service()
    container_client = service.get_container_client(nppes_container)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
        container_client.get_blob_client(zip_blob_name).download_blob().readinto(tmp)
    full_load = is_full_load(states)
    batch: list[InsertOne] = []
    inserted = 0

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            pl_name = next((n for n in zf.namelist() if n.lower().startswith("pl_pfile") and n.lower().endswith(".csv")), None)
            if not pl_name:
                return {"source": "pl_pfile", "inserted": 0, "warning": "pl_pfile missing"}
            with zf.open(pl_name) as raw:
                text = (line.decode("utf-8", errors="replace") for line in raw)
                reader = csv.DictReader(text)
                for row in reader:
                    npi = (row.get("NPI") or "").strip()
                    if not npi:
                        continue
                    addr = {
                        "line1": (row.get("Provider Secondary Practice Location Address- Address Line 1") or "").strip(),
                        "city": (row.get("Provider Secondary Practice Location Address - City Name") or "").strip(),
                        "state": (row.get("Provider Secondary Practice Location Address - State Name") or "").strip(),
                        "zip": ((row.get("Provider Secondary Practice Location Address - Postal Code") or "").strip())[:5],
                    }
                    addr = {k: v for k, v in addr.items() if v}
                    if not addr:
                        continue
                    if not full_load and not _raw_row_matches_state(row, states):
                        continue
                    batch.append(InsertOne({"npi": npi, "run_id": rt.run_id, "address": addr}))
                    if len(batch) >= 1000:
                        coll.bulk_write(batch, ordered=False)
                        inserted += len(batch)
                        batch = []
        if batch:
            coll.bulk_write(batch, ordered=False)
            inserted += len(batch)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    return {"source": "pl_pfile", "inserted": inserted}


def _load_reference_staging(ctx, rt, source_key: str, loader_name: str) -> dict:
    coll = rt.staging_coll(source_key)
    if not ctx.args.incremental:
        coll.drop()
    cfg = {**ctx.config, "env_prefix": ctx.env_prefix, "states": ctx.args.resolved_states()}
    if loader_name == "zip_crosswalk":
        from zip_county_crosswalk_loader import load_crosswalk
        result = load_crosswalk(cfg)
        docs = result.get("documents") or []
    elif loader_name == "rucc":
        from urban_flag import stamp_urban_flag_fn
        result = stamp_urban_flag_fn(cfg)
        docs = result.get("documents") or []
    elif loader_name == "specialty_catalog":
        from load_specialty_data import run_load_specialty_data
        result = run_load_specialty_data(cfg)
        docs = result.get("documents") or []
    elif loader_name == "nucc_taxonomy":
        from specialty_classification_catalog import load_catalog
        catalog = load_catalog()
        docs = [{"Code": code, **flags} for code, flags in catalog.items()]
    else:
        return {"source": source_key, "inserted": 0}
    if docs:
        coll.insert_many(docs, ordered=False)
    return {"source": source_key, "inserted": len(docs), "loader": loader_name}


def load_all_staging_sources(ctx, states: list[str]) -> dict:
    from pipeline_runtime import PipelineRuntime

    rt = PipelineRuntime(ctx)
    results = {
        "nppes_npi": _load_nppes_staging(ctx, rt, states),
        "pl_pfile": _load_pl_pfile_staging(ctx, rt, states),
        "zip_county_crosswalk": _load_reference_staging(ctx, rt, "zip_county_crosswalk", "zip_crosswalk"),
        "rucc": _load_reference_staging(ctx, rt, "rucc", "rucc"),
        "specialty_catalog": _load_reference_staging(ctx, rt, "specialty_catalog", "specialty_catalog"),
        "nucc_taxonomy": _load_reference_staging(ctx, rt, "nucc_taxonomy", "nucc_taxonomy"),
    }
    ctx.manifest.metrics["staging_load"] = {k: v.get("inserted", 0) for k, v in results.items()}
    return results
