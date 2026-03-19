"""Provider Load Manager — orchestrator and all activity implementations.

All I/O lives in activity functions. The orchestrator is deterministic
and replayable (no direct I/O).
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
from azure.storage.blob import BlobServiceClient
from bs4 import BeautifulSoup
from pymongo import MongoClient

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


# ── Helpers ───────────────────────────────────────────────────────────────────

def _blob_service() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(
        os.environ["AZURE_STORAGE_CONNECTION_STRING"]
    )


def _ensure_container(container: str) -> None:
    try:
        _blob_service().get_container_client(container).create_container()
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
    download_result = yield context.call_activity("download_zip_activity", config)
    zip_path = download_result["zip_path"]
    config = {**config, "version": download_result["version"]}

    # Step 2: Extract CSV from zip to blob
    csv_path = yield context.call_activity(
        "extract_csv_activity", {**config, "zip_path": zip_path}
    )

    # Step 3: Compute byte-aligned partitions
    partitions = yield context.call_activity(
        "partition_file_activity", {**config, "csv_path": csv_path}
    )

    # Step 4: Clear staging collection — remove all existing records before this load
    yield context.call_activity("clear_staging_activity", config)

    # Step 5: Ensure staging indexes exist before workers start (idempotent)
    yield context.call_activity("ensure_indexes_activity", config)

    # Step 6: Write one metadata record per worker
    metadata_ids = yield context.call_activity(
        "write_metadata_activity",
        {**config, "csv_path": csv_path, "partitions": partitions},
    )

    # Step 6: Fan-out — each worker+enrichment pair is a sub-orchestrator
    # Each pair starts immediately and chains enrichment on completion.
    pair_tasks = [
        context.call_sub_orchestrator(
            "worker_enrichment_pair",
            {**config, "csv_path": csv_path, "metadata_id": metadata_ids[i], **partition},
        )
        for i, partition in enumerate(partitions)
    ]
    pair_results = yield context.task_all(pair_tasks)

    # Step 7: Reconcile — count newlines in CSV vs loaded records
    reconcile_result = yield context.call_activity(
        "reconcile_activity",
        {**config, "csv_path": csv_path, "pair_results": pair_results},
    )

    # Step 8: Report — Excel → blob → SAS URL → Pushover
    yield context.call_activity(
        "report_activity",
        {**config, "pair_results": pair_results, "reconcile_result": reconcile_result},
    )

    total = sum(r.get("worker", {}).get("num_records", 0) for r in pair_results)
    any_failed = any(not r.get("worker", {}).get("success", True) for r in pair_results)
    # Per 2.5: mark entire load FAILED if any worker failed.
    # Successful inserts are NOT rolled back — load_id isolates this run.
    return {
        "status": "failed" if any_failed else "complete",
        "records_loaded": total,
    }


def worker_enrichment_pair_fn(context: df.DurableOrchestrationContext):
    """Sub-orchestrator: load one chunk, then immediately enrich it."""
    config = context.get_input()

    worker_result = yield context.call_activity("provider_worker_activity", config)

    enrich_result = yield context.call_activity(
        "county_enrich_activity", {**config, "worker_result": worker_result}
    )

    return {"worker": worker_result, "enrich": enrich_result}


# ── Activity implementations ──────────────────────────────────────────────────

def download_zip_fn(config: dict) -> dict:
    """Discover (if needed) and stream NPI zip from CMS to Azure Blob.

    If file_url is not in config, scrapes cms.gov/nppes to find the latest file.
    Falls back to NPPES_FALLBACK_URL if scraping fails.
    Returns {\"zip_path\": blob_name, \"version\": version} so the orchestrator
    can propagate the discovered version to downstream activities.
    """
    file_url = config.get("file_url")
    version = config.get("version")
    container = config.get("blob_container", "provider-data")

    if not file_url:
        try:
            file_url, version = _discover_nppes_url()
        except Exception as exc:
            logging.warning(
                "CMS NPPES auto-discovery failed (%s). "
                "Falling back to last-known URL: %s",
                exc, NPPES_FALLBACK_URL,
            )
            file_url = NPPES_FALLBACK_URL
            version = version or "fallback"

    blob_name = f"npi_{version}.zip"
    _ensure_container(container)
    service = _blob_service()
    blob_client = service.get_container_client(container).get_blob_client(blob_name)

    logging.info("Downloading NPI zip from %s", file_url)
    with requests.get(file_url, stream=True, timeout=600) as r:
        r.raise_for_status()
        blob_client.upload_blob(r.raw, overwrite=True)

    logging.info("ZIP uploaded to blob: %s", blob_name)
    return {"zip_path": blob_name, "version": version}


def extract_csv_fn(config: dict) -> str:
    """Extract CSV from zip blob, upload CSV blob. Returns csv blob name."""
    zip_blob_name = config["zip_path"]
    container = config.get("blob_container", "provider-data")
    version = config.get("version", "latest")
    csv_blob_name = f"npi_{version}.csv"

    service = _blob_service()
    container_client = service.get_container_client(container)

    # Download zip to temp file (zip is ~500MB, fits in /tmp on Azure Functions)
    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp_path = tmp.name
        container_client.get_blob_client(zip_blob_name).download_blob().readinto(tmp)

    # Stream-extract CSV directly to blob (8GB CSV never touches /tmp)
    csv_blob = container_client.get_blob_client(csv_blob_name)
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

    service = _blob_service()
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
        window = blob_client.download_blob(offset=nominal, length=1024).readall()
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


def clear_staging_fn(config: dict) -> dict:
    """Delete all documents from staging collection before a new load."""
    staging_collection = config.get(
        "staging_collection", "PublicHealthData.providers_staging"
    )
    db_name, coll_name = staging_collection.split(".", 1)
    client = MongoClient(os.environ["MONGO_connectionString"])
    try:
        result = client[db_name][coll_name].delete_many({})
        logging.info(
            "Cleared staging collection %s: %d documents deleted",
            staging_collection, result.deleted_count,
        )
        return {"deleted_count": result.deleted_count}
    finally:
        client.close()


def ensure_indexes_fn(config: dict) -> None:
    """Create staging collection indexes before workers start. Idempotent."""
    staging_collection = config.get(
        "staging_collection", "PublicHealthData.providers_staging"
    )
    db_name, coll_name = staging_collection.split(".", 1)
    client = MongoClient(os.environ["MONGO_connectionString"])
    try:
        collection = client[db_name][coll_name]
        collection.create_index(
            [("load_id", 1), ("record_id", 1)],
            unique=True,
            background=True,
            name="load_record_unique",
        )
        logging.info("Indexes ensured on %s", staging_collection)
    finally:
        client.close()


def write_metadata_fn(config: dict) -> list:
    """Write one metadata record per worker. Returns list of inserted _id strings."""
    partitions = config["partitions"]
    csv_path = config["csv_path"]
    version = config.get("version", "latest")
    load_id = config.get("load_id")
    metadata_collection = config.get("metadata_collection", "admin.DataLoadMetadata")
    now = datetime.now(timezone.utc).isoformat()

    db_name, coll_name = metadata_collection.split(".", 1)
    client = MongoClient(os.environ["MONGO_connectionString"])
    try:
        collection = client[db_name][coll_name]
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
                "status": 0,
                "error_detail": None,
            }
            for p in partitions
        ]
        result = collection.insert_many(docs)
    finally:
        client.close()

    return [str(oid) for oid in result.inserted_ids]


def reconcile_fn(config: dict) -> dict:
    """Stream CSV counting newlines. Compare to sum of worker num_records."""
    csv_path = config["csv_path"]
    container = config.get("blob_container", "provider-data")
    pair_results = config.get("pair_results", [])

    service = _blob_service()
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

    loaded_count = sum(
        r.get("worker", {}).get("num_records", 0) for r in pair_results
    )
    failed_records = sum(
        r.get("worker", {}).get("rows_failed", 0) for r in pair_results
    )
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


def report_fn(config: dict) -> dict:
    from discrepancy_reporter import DiscrepancyReporter
    return DiscrepancyReporter().send(config)


def county_enrich_fn(config: dict) -> dict:
    from county_enrichment_job import CountyEnrichmentJob
    return CountyEnrichmentJob().enrich(config)


def provider_worker_fn(config: dict) -> dict:
    from provider_worker import ProviderWorker
    return ProviderWorker(config).run()
