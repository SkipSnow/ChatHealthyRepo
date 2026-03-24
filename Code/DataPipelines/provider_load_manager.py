# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""Provider Load Manager — orchestrator and all activity implementations.

All I/O lives in activity functions. The orchestrator is deterministic
and replayable (no direct I/O).

Pipeline steps (referenced by start_step config key):
  1 — Download, extract, and partition NPPES zip → Blob Storage
  2 — Load partitions into providers_staging (fan-out across num_workers)
  3 — County enrichment Pass 1: out-of-scope filter + ZIP-code bulk lookup
  4 — County enrichment Pass 2: Census Geocoder batch (primary location)
  5 — County enrichment Pass 3: billing address retry
  6 — Generate provider embeddings + create Atlas Vector Search index
"""

import csv
import io
import logging
import os
import re
import tempfile
import zipfile
from datetime import datetime, timezone

import azure.durable_functions as df
import requests
from blob_client import get_blob_service
from bs4 import BeautifulSoup
from data_fetcher_base import DataFetcherBase
from pymongo import MongoClient


_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    # Module-level singleton — reused across activity function calls on the same instance.
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(os.environ["MONGO_connectionString"])
    return _mongo


# ── NPPES source URL ──────────────────────────────────────────────────────────

NPPES_INDEX_URL = "https://download.cms.gov/nppes/NPI_Files.html"

# Last-known URL used as fallback when CMS page scraping fails.
# Update this whenever the scraper is repaired or a manual download is done.
NPPES_FALLBACK_URL = (
    "https://download.cms.gov/nppes/NPPES_Data_Dissemination_April_2025.zip"
)


def _discover_nppes_url() -> tuple[str, str]:
    """Scrape CMS NPPES page and return (zip_url, version_string) for the latest full file."""
    resp = requests.get(NPPES_INDEX_URL, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    for link in soup.find_all("a", href=True):
        href = link["href"]
        # Full dissemination file — matches e.g. NPPES_Data_Dissemination_March_2026.zip
        match = re.search(
            r"NPPES_Data_Dissemination_(\w+_\d{4})\.zip", href, re.IGNORECASE
        )
        if match:
            version = match.group(1).replace("_", "-").lower()  # e.g. "march-2026"
            url = href if href.startswith("http") else f"https://download.cms.gov/nppes/{href}"
            logging.info("Discovered NPPES file: %s (version: %s)", url, version)
            return url, version

    raise RuntimeError("Could not find NPPES full dissemination zip on CMS page.")


class NppesFetcher(DataFetcherBase):
    """NPPES NPI full dissemination zip fetcher.

    Auto-discovers the current month's URL from the CMS index page.
    Falls back to NPPES_FALLBACK_URL if scraping fails.
    """
    source_name = "nppes_npi"

    def __init__(self, config: dict = None):
        super().__init__(config)
        # Discover URL and version at construction time
        try:
            self.source_url, self._version = _discover_nppes_url()
        except Exception as exc:
            logging.warning(
                "NPPES auto-discovery failed (%s). Using fallback URL.", exc
            )
            self.source_url = NPPES_FALLBACK_URL
            self._version = config.get("version", "fallback") if config else "fallback"

    def blob_name(self) -> str:
        return f"npi_{self._version}.zip"

    @property
    def version(self) -> str:
        return self._version


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure_container(container: str) -> None:
    try:
        get_blob_service().get_container_client(container).create_container()
    except Exception:
        pass  # already exists


# ── Orchestrators ─────────────────────────────────────────────────────────────

def provider_load_orchestrator_fn(context: df.DurableOrchestrationContext):
    """Main orchestrator. No timeouts — Azure manages state."""
    config = context.get_input()

    # Propagate load_id (= orchestration instance_id) through all activities.
    # Used for idempotency on retry: unique index on (load_id, record_id).
    load_id = context.instance_id
    config = {**config, "load_id": load_id}

    # Step 1: Download zip to blob (auto-discovers URL + version if not supplied)
    context.set_custom_status("Step 1/10: Downloading NPI zip from CMS")
    download_result = yield context.call_activity("download_zip_activity", config)
    zip_path = download_result["zip_path"]
    config = {**config, "version": download_result["version"]}

    # Step 2: Extract CSV from zip to blob
    context.set_custom_status(f"Step 2/10: Extracting CSV (version: {config['version']})")
    csv_path = yield context.call_activity(
        "extract_csv_activity", {**config, "zip_path": zip_path}
    )

    # Step 3: Compute byte-aligned partitions
    context.set_custom_status("Step 3/10: Partitioning file")
    partitions = yield context.call_activity(
        "partition_file_activity", {**config, "csv_path": csv_path}
    )

    # Step 4: Drain staging — drop all existing records before loading new data.
    # New data is verified viable (download + extract + partition succeeded) before we drop.
    context.set_custom_status("Step 4/10: Draining staging collection")
    yield context.call_activity("drain_staging_activity", config)

    # Step 5: Pre-load index only — unique compound index for idempotency on retry.
    # Secondary indexes are deferred to Step 8 (post-load) to eliminate write
    # amplification during the bulk insert phase.
    context.set_custom_status("Step 5/10: Ensuring pre-load index")
    yield context.call_activity("ensure_preload_indexes_activity", config)

    # Step 6: Write one metadata record per worker
    context.set_custom_status(f"Step 6/10: Writing metadata ({len(partitions)} workers)")
    metadata_ids = yield context.call_activity(
        "write_metadata_activity",
        {**config, "csv_path": csv_path, "partitions": partitions},
    )

    # Step 6.5: Pre-warm Flex Consumption instances before fan-out.
    # Sets always_ready = num_workers via Azure Management API (MSI auth), then waits
    # 3× PATCH latency for propagation. Prevents cold-start stacking (OOM, two-wave pattern).
    # Non-fatal if MSI role not yet assigned — warm-up is skipped and fan-out proceeds.
    context.set_custom_status(f"Step 6.5/10: Pre-warming {config.get('num_workers', 32)} instances")
    warm_metrics = yield context.call_activity("warm_instances_activity", config)
    logging.info("Warm-up metrics: %s", warm_metrics)

    # Step 7: Fan-out — all workers run simultaneously (no chunking).
    # 32 partitions → 32 concurrent MongoDB writers → ~25% WiredTiger write ticket utilization.
    # Sub-orchestrators are avoided (direct activity calls) to prevent Durable Functions
    # history explosion (~15K events for 200 workers vs ~600 with direct calls).
    context.set_custom_status(f"Step 7/10: Loading — {len(partitions)} workers")
    worker_tasks = [
        context.call_activity(
            "provider_worker_activity",
            {**config, "csv_path": csv_path, "metadata_id": metadata_ids[i], **partition},
        )
        for i, partition in enumerate(partitions)
    ]
    worker_results = yield context.task_all(worker_tasks)

    # Step 7.5: Reset always_ready = 0 — no standby cost between runs.
    yield context.call_activity("cool_instances_activity", config)

    # Steps 8+9 run in parallel: index build (MongoDB) and reconcile (blob read) are independent.
    context.set_custom_status("Step 8-9/10: Building indexes + reconciling (parallel)")
    postload_task = context.call_activity("ensure_postload_indexes_activity", config)
    reconcile_task = context.call_activity(
        "reconcile_activity",
        {**config, "csv_path": csv_path, "worker_results": worker_results},
    )
    parallel_results = yield context.task_all([postload_task, reconcile_task])
    reconcile_result = parallel_results[1]

    # Step 10: Report — write to admin.PipelineDiscrepancyReport → SparkPost email
    context.set_custom_status("Step 10/10: Writing report")
    yield context.call_activity(
        "report_activity",
        {**config, "worker_results": worker_results, "reconcile_result": reconcile_result},
    )

    total = sum(r.get("num_records", 0) for r in worker_results)
    any_failed = any(not r.get("success", True) for r in worker_results)
    # Per 2.5: mark entire load FAILED if any worker failed.
    # Successful inserts are NOT rolled back — load_id isolates this run.
    status = "failed" if any_failed else "complete"
    context.set_custom_status(f"Done — {status}, {total:,} records loaded")
    return {
        "status": status,
        "records_loaded": total,
    }


# ── Activity implementations ──────────────────────────────────────────────────

def download_zip_fn(config: dict) -> dict:
    """Fetch NPI zip from CMS to Azure Blob via NppesFetcher.

    Uses ETag/checksum guard — if the file is unchanged since the last
    download, logs "already landed" and returns the existing blob path
    without re-downloading. Returns {"zip_path": blob_name, "version": version}.
    """
    fetcher = NppesFetcher(config)
    result = fetcher.fetch()

    if result["skipped"]:
        logging.info(
            "NPI zip already landed (version: %s, blob: %s) — skipping download.",
            result["version"], result["blob_path"],
        )
    else:
        logging.info(
            "NPI zip downloaded (version: %s, blob: %s, sha256: %s…).",
            result["version"], result["blob_path"], result["checksum_sha256"][:16],
        )

    return {"zip_path": result["blob_path"], "version": result["version"]}


def extract_csv_fn(config: dict) -> str:
    """Extract CSV from zip blob, upload CSV blob. Returns csv blob name.

    Skip guard: if npi_{version}.csv already exists in blob, skip extraction.
    The NPPES zip is ~8-9GB — re-extracting on every run costs ~60-90 minutes.
    """
    zip_blob_name = config["zip_path"]
    container = config.get("blob_container", "provider-data")
    version = config.get("version", "latest")
    csv_blob_name = f"npi_{version}.csv"

    service = get_blob_service()
    container_client = service.get_container_client(container)

    # Skip if CSV already exists for this version
    csv_blob = container_client.get_blob_client(csv_blob_name)
    try:
        csv_blob.get_blob_properties()
        logging.info("CSV already exists in blob: %s — skipping extraction.", csv_blob_name)
        return csv_blob_name
    except Exception:
        pass  # blob does not exist — proceed with extraction

    # Download zip to temp file, then stream-extract CSV to blob
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
        container_client.get_blob_client(zip_blob_name).download_blob().readinto(tmp)

    # Stream-extract CSV directly to blob (8GB+ CSV never touches /tmp)
    with zipfile.ZipFile(tmp_path) as zf:
        csv_name = next(
            n for n in zf.namelist()
            if n.lower().endswith(".csv") and "npidata" in n.lower()
        )
        with zf.open(csv_name) as csv_stream:
            csv_blob.upload_blob(csv_stream, overwrite=True)

    os.unlink(tmp_path)
    logging.info("CSV extracted to blob: %s", csv_blob_name)
    return csv_blob_name


def partition_file_fn(config: dict) -> list:
    """Compute byte-aligned partitions. Returns list of partition dicts."""
    num_workers = config.get("num_workers", 5)
    max_bytes = config.get("max_bytes")  # None = full file; pass explicitly for dev/testing
    csv_path = config["csv_path"]
    container = config.get("blob_container", "provider-data")

    service = get_blob_service()
    blob_client = service.get_container_client(container).get_blob_client(csv_path)
    file_size = blob_client.get_blob_properties().size

    # Read header row — NPI CSV header is ~12KB (330 columns × ~35 bytes each).
    # Read 32KB to guarantee we capture the full header line regardless of file version.
    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    if "\n" not in header_text:
        raise RuntimeError("Header line exceeds 32KB — unexpected CSV format.")
    header_line = header_text.split("\n")[0]
    header = list(csv.reader([header_line]))[0]
    header_end = len(header_line.encode("utf-8")) + 1  # +1 for \n
    logging.info("Header parsed: %d fields, header_end=%d", len(header), header_end)

    if max_bytes:
        effective_end = min(file_size, header_end + max_bytes)
    else:
        effective_end = file_size

    data_size = effective_end - header_end
    chunk_size = data_size // num_workers

    # Compute newline-aligned boundaries so no record is split or skipped.
    # ASSUMPTION: NPPES CSV does NOT contain embedded newlines in quoted fields.
    # This is empirically consistent with CMS data. If violated, workers will
    # produce malformed rows (wrong field count) which are counted as rows_failed,
    # and the reconciliation step will detect the resulting record-count mismatch.
    # Scan 1024 bytes (NPI rows average ~500 bytes; 1024 guarantees finding \n).
    boundaries = [header_end]
    for i in range(1, num_workers):
        nominal = header_end + i * chunk_size
        window = blob_client.download_blob(offset=nominal, length=65536).readall()
        newline_pos = window.find(b"\n")
        true_boundary = nominal + newline_pos + 1 if newline_pos >= 0 else nominal
        boundaries.append(true_boundary)
    boundaries.append(effective_end)

    partitions = []
    for i in range(num_workers):
        partitions.append({
            "worker_id": i + 1,
            "start_byte": boundaries[i],
            "end_byte": boundaries[i + 1],
            "header": header,
        })

    logging.info("Computed %d partitions from %d bytes of data", len(partitions), data_size)
    return partitions



def ensure_preload_indexes_fn(_config: dict) -> None:
    """No-op — unique index deferred to post-load for performance.
    Pure insertMany during load requires no pre-existing unique index.
    drain_staging guarantees an empty collection before workers start,
    so duplicate inserts cannot occur on the happy path.
    """
    logging.info("Pre-load index step: no-op (index deferred to post-load)")


def ensure_postload_indexes_fn(config: dict) -> None:
    """Create all indexes after load completes. Idempotent.
    Building indexes over existing data in one pass is significantly faster
    than maintaining them incrementally during bulk insert.
    Also called at the start of county enrichment to guarantee indexes
    exist when CountyEnrichment is run standalone.
    """
    staging_collection = config.get(
        "staging_collection", "PublicHealthData.providers_staging"
    )
    db_name, coll_name = staging_collection.split(".", 1)
    collection = _get_mongo_client()[db_name][coll_name]
    collection.create_index(
        [("load_id", 1), ("record_id", 1)],
        unique=True,
        name="load_record_unique",
    )
    collection.create_index(
        "practice_address.zip",
        name="practice_zip",
    )
    collection.create_index(
        "county.fips",
        name="county_fips",
    )
    collection.create_index(
        "worker_id",
        name="worker_id",
    )
    logging.info("Post-load indexes ensured on %s", staging_collection)


def write_metadata_fn(config: dict) -> list:
    """Write one metadata record per worker. Returns list of inserted _id strings."""
    from provider_worker import status_fields
    partitions = config["partitions"]
    csv_path = config["csv_path"]
    version = config.get("version", "latest")
    load_id = config.get("load_id")
    metadata_collection = config.get("metadata_collection", "admin.DataLoadMetadata")
    now = datetime.now(timezone.utc).isoformat()

    db_name, coll_name = metadata_collection.split(".", 1)
    collection = _get_mongo_client()[db_name][coll_name]
    docs = [
        {
            "type": "ProviderData",
            "load_id": load_id,
            "date": now,
            "version": version,
            "worker_id": p["worker_id"],
            "file_location": csv_path,
            "start_byte": p["start_byte"],
            "end_byte": p["end_byte"],
            "num_records": 0,
            **status_fields(0),
            "error_detail": None,
        }
        for p in partitions
    ]
    result = collection.insert_many(docs)
    return [str(oid) for oid in result.inserted_ids]


def reconcile_fn(config: dict) -> dict:
    """Stream CSV counting newlines. Compare to sum of worker num_records."""
    csv_path = config["csv_path"]
    container = config.get("blob_container", "provider-data")

    service = get_blob_service()
    blob_client = service.get_container_client(container).get_blob_client(csv_path)

    total_newlines = 0
    last_chunk = b""
    for chunk in blob_client.download_blob().chunks():
        total_newlines += chunk.count(b"\n")
        if chunk:
            last_chunk = chunk

    # Each row (including header) ends with \n → total_newlines = N_data + 1.
    # If the file has no trailing newline on the last row, add 1 to compensate.
    csv_record_count = total_newlines - 1  # subtract header row
    if last_chunk and last_chunk[-1:] != b"\n":
        csv_record_count += 1

    worker_results = config.get("worker_results", [])
    loaded_count = sum(r.get("num_records", 0) for r in worker_results)
    failed_records = sum(r.get("rows_failed", 0) for r in worker_results)
    match = csv_record_count == loaded_count

    logging.info(
        "Reconciliation: expected=%d  inserted=%d  failed=%d  Match=%s",
        csv_record_count, loaded_count, failed_records, match,
    )
    return {
        "expected_records": csv_record_count,
        "inserted_records": loaded_count,
        "failed_records": failed_records,
        "match": match,
    }


def drain_staging_fn(config: dict) -> dict:
    """Drop providers_staging before loading new data.

    Uses drop() instead of delete_many() — drop immediately returns disk space
    to Atlas. delete_many() leaves WiredTiger holding the allocated space as
    fragmented free pages, which does not reduce billed storage.

    Called after download/extract/partition succeed so we know the new data is viable.
    """
    staging_collection = config.get("staging_collection", "PublicHealthData.providers_staging")
    db_name, coll_name = staging_collection.split(".", 1)
    _get_mongo_client()[db_name][coll_name].drop()
    logging.info("Dropped staging collection %s — disk space returned to Atlas.", staging_collection)
    return {"drained": True}


def report_fn(config: dict) -> dict:
    from discrepancy_reporter import DiscrepancyReporter
    return DiscrepancyReporter().send(config)


def provider_worker_fn(config: dict) -> dict:
    from provider_worker import ProviderWorker
    return ProviderWorker(config).pipeline_execute()


def embed_worker_fn(config: dict) -> dict:
    from embedding_worker import EmbeddingWorker
    return EmbeddingWorker(config).pipeline_execute()


def create_vector_index_fn(config: dict) -> dict:
    """Create Atlas Vector Search index on providers_staging. Idempotent."""
    staging_collection = config.get("staging_collection", "PublicHealthData.providers_staging")
    db_name, coll_name = staging_collection.split(".", 1)
    collection = _get_mongo_client()[db_name][coll_name]
    index_name = "provider_vector_index"
    try:
        collection.create_search_index({
            "name": index_name,
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 1536,
                        "similarity": "cosine",
                    }
                ]
            },
        })
        logging.info("Vector search index '%s' created on %s", index_name, staging_collection)
    except Exception as exc:
        if "already exists" in str(exc).lower() or "duplicate" in str(exc).lower():
            logging.info("Vector search index '%s' already exists — skipping.", index_name)
        else:
            raise
    return {"index": index_name, "status": "ready"}


# ── Full Pipeline Orchestrator ────────────────────────────────────────────────

def full_provider_pipeline_orchestrator_fn(context: df.DurableOrchestrationContext):
    """Top-level orchestrator: health check → load → enrichment → embeddings.

    start_step (1–6): skip all steps before this number. Default 1 (full run).
    Pass start_step in the payload to resume after a partial failure.

    Steps:
      1 — MongoDB health check
      2 — Download + extract + load provider records
      3 — County enrichment Pass 1 (mark out_of_scope + ZIP bulk)
      4 — County enrichment Pass 2 (Census Geocoder batch)
      5 — County enrichment Pass 3 (billing address retry)
      6 — Generate embeddings + create Atlas Vector Search index
    """
    from county_enrichment_job import _build_enrichment_reconcile

    config = context.get_input() or {}
    load_id = context.instance_id
    start_step = config.get("start_step", 1)

    enrich_config = {
        "load_id": load_id,
        "num_workers": config.get("enrich_workers", 200),
        "addr_batch_size": config.get("addr_batch_size", 5_000),
        "reset_failed": config.get("reset_failed", False),
    }

    # Step 1: MongoDB health check
    if start_step <= 1:
        context.set_custom_status("Step 1/5: Checking MongoDB health")
        yield context.call_activity("check_mongo_health_activity", config)

    # Step 2: Load provider data
    load_result = {"status": "skipped"}
    if start_step <= 2:
        context.set_custom_status("Step 2/5: Loading provider data")
        load_config = {
            "num_workers": config.get("num_workers", 32),
            "batch_size": config.get("batch_size", 5000),
            "blob_container": config.get("blob_container", "provider-data"),
        }
        load_result = yield context.call_sub_orchestrator("provider_load_orchestrator", load_config)

    # Step 3: County enrichment — Pass 1 (mark out_of_scope + ZIP bulk)
    pass1_result = None
    if start_step <= 3:
        context.set_custom_status("Step 3/6: County enrichment — Pass 1 (ZIP bulk)")
        pass1_result = yield context.call_sub_orchestrator(
            "county_enrichment_pass1_orchestrator", enrich_config
        )

    # Step 4: County enrichment — Pass 2 (Census Geocoder batch)
    pass2_result = None
    if start_step <= 4:
        context.set_custom_status("Step 4/6: County enrichment — Pass 2 (Census Geocoder)")
        pass2_result = yield context.call_sub_orchestrator(
            "county_enrichment_pass2_orchestrator", enrich_config
        )

    # Step 5: County enrichment — Pass 3 (billing address retry)
    pass3_result = None
    if start_step <= 5:
        context.set_custom_status("Step 5/6: County enrichment — Pass 3 (billing address retry)")
        pass3_result = yield context.call_sub_orchestrator(
            "county_enrichment_pass3_orchestrator", enrich_config
        )

    # Enrichment reconcile report — runs after all passes, so pass3 is included
    reconcile = None
    if pass1_result is not None or pass2_result is not None or pass3_result is not None:
        reconcile = _build_enrichment_reconcile(
            pass1_result or {}, pass2_result or {}, pass3_result or {}
        )
        yield context.call_activity(
            "enrichment_report_activity", {**enrich_config, "reconcile": reconcile}
        )

    # Step 6: Generate embeddings + create Atlas Vector Search index
    # embedding_enabled must be explicitly True — default is False (embeddings are expensive
    # and require OpenAI; skip unless intentionally requested).
    embed_results = []
    if start_step <= 6 and config.get("embedding_enabled", False):
        num_workers = config.get("num_workers", 32)
        staging_collection = config.get("staging_collection", "PublicHealthData.providers_staging")
        context.set_custom_status(f"Step 6/6: Generating embeddings ({num_workers} workers)")
        embed_tasks = [
            context.call_activity(
                "embed_worker_activity",
                {"worker_id": i + 1, "staging_collection": staging_collection},
            )
            for i in range(num_workers)
        ]
        embed_results = yield context.task_all(embed_tasks)

        context.set_custom_status("Step 6/6: Creating vector search index")
        yield context.call_activity(
            "create_vector_index_activity", {"staging_collection": staging_collection}
        )

    total_embedded = sum(r.get("embedded", 0) for r in embed_results)
    pass3_enriched = pass3_result.get("pass3_modified", 0) if pass3_result else 0
    enrich_status = (
        "complete" if reconcile and reconcile["match"]
        else "partial" if reconcile
        else "skipped"
    )
    context.set_custom_status(
        f"Done — load {load_result.get('status', 'unknown')}, "
        f"enrichment {enrich_status}, "
        f"pass3 billing {pass3_enriched:,}, "
        f"embedded {total_embedded:,}"
    )
    return {
        "load": load_result,
        "enrichment": reconcile,
        "pass3": pass3_result,
        "embeddings": {"total_embedded": total_embedded},
    }


