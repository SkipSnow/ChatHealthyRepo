# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""Provider Load Manager — orchestrator and all activity implementations.

All I/O lives in activity functions. The orchestrator is deterministic
and replayable (no direct I/O).

This module hosts the prepare/load/normalize/multi-practice/embedding
sub-orchestrators and their supporting activity functions, plus the
PREP_LABEL_* / LOAD_LABEL_* status strings each sub-orchestrator
publishes via set_custom_status. The retired step-driven parent
orchestrator (ProviderPipelineOrchestrator) and its 13-step label
inventory were removed 2026-05-28; the live provider pipeline parent
is provider_pipeline_orchestrator_fn in function_app.py and runs
end-to-end without start_step semantics.
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
from base_pipeline_orchestrator import BasePipelineOrchestrator
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


def _discover_nppes_url() -> tuple[str, str]:
    """Find the latest NPPES full-dissemination zip URL via AI-agent discovery.

    F-102-S-003-REQ-T-006. No regex, no fallback constant — the agent reads the
    CMS page and picks the latest full file, handling version-stamps (V1, V2,
    V3, ...) and ignoring weekly diffs and deactivated-NPI reports. On failure
    this function raises; the caller MUST NOT silently substitute a stale URL.

    Returns (zip_url, version_string). The version_string is derived from the
    filename and used to name the blob; if the filename is anomalous we fall
    back to a hash of the URL so the blob name stays unique per release.
    """
    from source_url_discovery import find_latest_data_url

    instructions = (
        "Find the URL of the latest **full monthly** NPPES NPI Dissemination "
        "zip file. Rules: "
        "(a) Look for files whose name starts with "
        "`NPPES_Data_Dissemination_<Month>_<Year>` and ends with `.zip`. "
        "(b) The publisher sometimes appends a version stamp like `_V2`, "
        "`_V3`, etc. When multiple version stamps exist for the same month, "
        "return the HIGHEST version number for the LATEST month. "
        "(c) IGNORE filenames that contain `Weekly` (those are incremental "
        "diff files, not full files). "
        "(d) IGNORE filenames that contain `Deactivated` (those are "
        "deactivated-NPI reports, not provider data). "
        "(e) Return only the absolute URL of the chosen file."
    )

    url = find_latest_data_url(
        source_name="nppes_npi",
        page_url=NPPES_INDEX_URL,
        instructions=instructions,
    )

    # Derive a stable version slug from the filename so blob naming is
    # deterministic across re-runs of the same release.
    filename = url.rsplit("/", 1)[-1]
    version = filename.replace("NPPES_Data_Dissemination_", "").replace(".zip", "")
    if not version:
        # Pathological case — anomalous filename. Hash the URL for uniqueness.
        import hashlib
        version = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]

    logging.info("NPPES discovery: agent selected %s (version=%s)", url, version)
    return url, version


class NppesFetcher(DataFetcherBase):
    """NPPES NPI full dissemination zip fetcher.

    URL discovered by AI agent only when cache is missing or older than
    MAX_CACHE_AGE_DAYS. CMS posts a weekly incremental + monthly full
    dissemination; 7-day TTL keeps the full file fresh within one cycle.
    """
    source_name = "nppes_npi"
    MAX_CACHE_AGE_DAYS = 7

    def __init__(self, config: dict = None):
        super().__init__(config)
        self.source_url = ""
        self._version = ""

    def _resolve_source_url(self) -> str:
        self.source_url, self._version = _discover_nppes_url()
        return self.source_url

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


# Step 2 (Prepare data) internal stage labels — set_custom_status only.
PREP_LABEL_DOWNLOAD      = "Step 2a: Downloading NPI zip from CMS"
PREP_LABEL_EXTRACT       = "Step 2b: Extracting CSV"
PREP_LABEL_PARTITION     = "Step 2c: Partitioning file"
PREP_LABEL_DRAIN         = "Step 2d: Draining staging collection"
PREP_LABEL_DRAIN_SKIP    = "Step 2d: Skipping drain (incremental=true)"
PREP_LABEL_PRELOAD_IDX   = "Step 2e: Ensuring pre-load index"
PREP_LABEL_PRELOAD_SKIP  = "Step 2e: Skipping pre-load index (full load)"
PREP_LABEL_METADATA      = "Step 2f: Writing metadata"

# Step 4 (Load raw provider rows) internal stage labels.
LOAD_LABEL_WORKERS  = "Step 4: Raw-load worker fan-out"
LOAD_LABEL_INDEXES  = "Step 4: Building indexes + reconciling"
LOAD_LABEL_REPORT   = "Step 4: Writing report"

# Step 6 (Multi-practice addresses) and Step 14 (Embeddings) status labels.
PROVIDER_LABEL_MULTI_PRACTICE = "Step 6: Attaching multi-practice addresses"
PROVIDER_LABEL_EMBED          = "Step 14: Generating provider embeddings"


# ── Orchestrators ─────────────────────────────────────────────────────────────

def prepare_data_orchestrator_fn(context: df.DurableOrchestrationContext):
    """Step 2 sub-orchestration — Prepare data.

    Acquires the NPPES source file in blob, extracts the CSV, computes the
    byte-aligned partitions every Step-4 worker will read, ensures pre-load
    indexes, and writes one worker-ledger row per partition. Returns the
    structured handoff Step 4 needs.

    Drain happens upstream in provider_pipeline_orchestrator_fn before this
    sub-orchestrator starts, so a mid-run terminate during download/extract/
    partition still leaves the destination correctly drained.
    """
    config = context.get_input() or {}
    load_id = config.get("load_id") or context.instance_id
    config = {**config, "load_id": load_id}

    context.set_custom_status(PREP_LABEL_DOWNLOAD)
    download_result = yield context.call_activity("download_zip_activity", config)
    zip_path = download_result["zip_path"]
    version = download_result["version"]
    config = {**config, "version": version}

    context.set_custom_status(PREP_LABEL_EXTRACT)
    csv_path = yield context.call_activity(
        "extract_csv_activity", {**config, "zip_path": zip_path}
    )

    context.set_custom_status(PREP_LABEL_PARTITION)
    partitions = yield context.call_activity(
        "partition_file_activity", {**config, "csv_path": csv_path}
    )

    # Pre-load index only matters for incremental loads (full loads drop the
    # collection in drain). Skip on full load so post-load index creation
    # doesn't trip DuplicateKeyError.
    if config.get("incremental", False):
        context.set_custom_status(PREP_LABEL_PRELOAD_IDX)
        yield context.call_activity("ensure_preload_indexes_activity", config)
    else:
        context.set_custom_status(PREP_LABEL_PRELOAD_SKIP)

    context.set_custom_status(PREP_LABEL_METADATA)
    metadata_ids = yield context.call_activity(
        "write_metadata_activity",
        {**config, "csv_path": csv_path, "partitions": partitions},
    )

    return {
        "load_id":      load_id,
        "version":      version,
        "zip_path":     zip_path,
        "csv_path":     csv_path,
        "partitions":   partitions,
        "metadata_ids": metadata_ids,
    }


def multi_practice_addresses_orchestrator_fn(context: df.DurableOrchestrationContext):
    """Step 6 sub-orchestration — Load multi practice addresses.

    Wraps the server-side `attach_practice_locations_activity` so the step is
    autonomous (own per-activity budget, own custom status).
    """
    config = context.get_input() or {}

    context.set_custom_status(PROVIDER_LABEL_MULTI_PRACTICE)
    result = yield context.call_activity("attach_practice_locations_activity", config)
    return result


def embeddings_orchestrator_fn(context: df.DurableOrchestrationContext):
    """Step 14 sub-orchestration — Embeddings.

    Fans out embed_worker_activity across num_workers, then stamps embedding
    metadata onto records. The downstream front-end migration job builds the
    vector index on the front-end cluster — the pipeline cluster is paused
    between runs so no live index lives here.
    """
    config = context.get_input() or {}
    num_workers = int(config.get("num_workers", 1))
    provider_collection = config.get("provider_collection", "dev_PublicHealthData.providers")

    context.set_custom_status(PROVIDER_LABEL_EMBED)
    embed_tasks = [
        context.call_activity(
            "embed_worker_activity",
            {
                "worker_id":            i + 1,
                "provider_collection":  provider_collection,
                "states":               config["states"],
                "embed_model":          config["embed_model"],
                "embed_batch_size":     config["embed_batch_size"],
                "embed_initial_jitter": config["embed_initial_jitter"],
                "throttle":             config["throttle"],
            },
        )
        for i in range(num_workers)
    ]
    embed_results = yield context.task_all(embed_tasks)

    total_embedded = sum(r.get("embedded", 0) for r in embed_results)
    total_tokens   = sum(r.get("total_tokens", 0) for r in embed_results)
    return {
        "total_embedded": total_embedded,
        "total_tokens":   total_tokens,
        "worker_results": embed_results,
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


def attach_practice_locations_fn(config: dict) -> dict:
    """Enrichment-phase activity: attach NPPES secondary practice locations
    to providers via a server-side `$merge`.

    Background — CMS NPPES Data Dissemination Readme §2.3:
      "NPPES now collects multiple Practice Location associated with Type 1
       and Type 2 NPIs. The Data File contains the first Primary Practice
       Location, and the Practice Location Reference File will contain all
       of the non-primary Practice Locations."

    Two phases, both server-side (no documents leave Atlas):
      A. Stream `pl_pfile_*.csv` from the NPPES zip in blob, drop+recreate the
         per-NPI lookup collection ({_id: NPI, addresses: [...]}). Live-only —
         no history retained.
      B. `providers.aggregate($lookup pl_pfile_lookup → $set practice_address →
         $merge providers)` — concatenates secondary addresses onto each
         provider's `practice_address` array, scoped by the run's state filter.

    Self-contained: the activity invokes `download_zip_fn(config)` (idempotent;
    skips if the zip is already in blob) so it works whether or not the load
    sub-orchestrator ran in the same invocation. This lets a caller resume the
    pipeline at `start_step="Step 5: Attaching secondary practice locations"`
    without requiring a fresh NPPES download.

    Returns: {"npis_with_secondary": int, "rows_loaded": int,
              "providers_modified": int, "lookup_collection": str}
    """
    import csv as _csv
    from state_filter import normalize_states, is_full_load, mongo_state_filter

    container = config.get("blob_container", "provider-data")
    pl_lookup_collection = config.get(
        "pl_lookup_collection", "dev_PublicHealthData.pl_pfile_lookup"
    )
    pl_db_name, pl_coll_name = pl_lookup_collection.split(".", 1)
    provider_collection = config.get(
        "provider_collection", "dev_PublicHealthData.providers"
    )
    prov_db_name, prov_coll_name = provider_collection.split(".", 1)

    # Phase 0: ensure the NPPES zip is in blob. download_zip_fn is idempotent —
    # it skips if the file is already landed for this version.
    download_result = download_zip_fn(config)
    zip_blob_name = download_result["zip_path"]
    version = download_result["version"]

    # Field-name map mirrors the published pl_pfile_*_fileheader.csv.
    PL_FIELDS = {
        "Provider Secondary Practice Location Address- Address Line 1":   "line1",
        "Provider Secondary Practice Location Address-  Address Line 2":  "line2",
        "Provider Secondary Practice Location Address - City Name":       "city",
        "Provider Secondary Practice Location Address - State Name":      "state",
        "Provider Secondary Practice Location Address - Postal Code":     "zip",
        "Provider Secondary Practice Location Address - Country Code (If outside U.S.)": "country",
        "Provider Secondary Practice Location Address - Telephone Number":            "phone",
        "Provider Secondary Practice Location Address - Telephone Extension":         "phone_ext",
        "Provider Practice Location Address -  Fax Number":              "fax",
    }

    service = get_blob_service()
    container_client = service.get_container_client(container)

    # Open the temp file with non-exclusive write and stream the blob in
    # explicit chunks so the Python worker's gRPC server thread can keep
    # responding to Functions-host heartbeats during the multi-GB download.
    # readinto() with a single call held the GIL long enough to cause
    # SIGTERM (exit 143). Concurrent download streams the chunks with
    # max_concurrency parallelism, and the per-chunk loop yields the GIL.
    fd, tmp_path = tempfile.mkstemp(suffix=".zip")
    os.close(fd)
    blob_client = container_client.get_blob_client(zip_blob_name)
    with open(tmp_path, "wb") as f:
        stream = blob_client.download_blob(max_concurrency=4)
        for chunk in stream.chunks():
            f.write(chunk)

    pl_csv_name = None
    with zipfile.ZipFile(tmp_path) as zf:
        for n in zf.namelist():
            low = n.lower()
            if low.startswith("pl_pfile") and low.endswith(".csv"):
                pl_csv_name = n
                break

    if pl_csv_name is None:
        os.unlink(tmp_path)
        # Fail loud — CMS publishes the Practice Location Reference File alongside
        # the main NPPES data; absence indicates a malformed source zip.
        raise RuntimeError(
            f"pl_pfile_*.csv not found in {zip_blob_name} (version {version}). "
            "CMS publishes the Practice Location Reference File alongside the "
            "main NPPES data; absence here indicates a malformed source zip."
        )

    # Phase A: drop + recreate the lookup collection (live-only contract).
    pl_coll = _get_mongo_client()[pl_db_name][pl_coll_name]
    pl_coll.drop()

    npi_to_addrs: dict[str, list[dict]] = {}
    rows_loaded = 0
    with zipfile.ZipFile(tmp_path) as zf:
        with zf.open(pl_csv_name) as raw:
            text = (line.decode("utf-8", errors="replace") for line in raw)
            reader = _csv.DictReader(text)
            for row in reader:
                npi = (row.get("NPI") or "").strip()
                if not npi:
                    continue
                addr = {}
                for src, sub in PL_FIELDS.items():
                    v = (row.get(src) or "").strip()
                    if v:
                        addr[sub] = v
                if not addr:
                    continue
                if "zip" in addr:
                    addr["zip"] = addr["zip"][:5]
                # Stamp address_type + county placeholder so the secondaries
                # are schema-conformant the moment they land in the lookup
                # collection — when the $merge concatenates them onto the
                # provider's existing addresses[] there's no remapping required.
                addr["address_type"] = "practice"
                addr["county"] = {"fips": None}
                npi_to_addrs.setdefault(npi, []).append(addr)
                rows_loaded += 1

    os.unlink(tmp_path)

    if npi_to_addrs:
        from pymongo import InsertOne
        ops = [InsertOne({"_id": npi, "addresses": addrs})
               for npi, addrs in npi_to_addrs.items()]
        BATCH = 1000
        for i in range(0, len(ops), BATCH):
            pl_coll.bulk_write(ops[i:i + BATCH], ordered=False)

    # Phase B: server-side `providers.aggregate → $merge providers` —
    # concat the pl_pfile secondary addresses onto each provider's
    # addresses[] array. State-scoped via the shared predicate every other
    # step uses (REQ-T-001). Post-schema-reconciliation: addresses[] is the
    # unified array (business + practice entries with address_type
    # discriminator); secondaries from pl_pfile arrive already shaped with
    # address_type='practice' and county={fips:null}.
    states = normalize_states(config)
    state_match = {} if is_full_load(states) else mongo_state_filter(states)

    prov_coll = _get_mongo_client()[prov_db_name][prov_coll_name]
    pipeline = [
        {"$match": state_match} if state_match else {"$match": {}},
        {"$lookup": {
            "from": pl_coll_name,
            "localField": "npi",
            "foreignField": "_id",
            "as": "_pl",
        }},
        {"$match": {"_pl.0": {"$exists": True}}},
        {"$set": {
            "addresses": {
                "$concatArrays": [
                    {"$ifNull": ["$addresses", []]},
                    {"$ifNull": [{"$arrayElemAt": ["$_pl.addresses", 0]}, []]},
                ],
            },
        }},
        {"$unset": "_pl"},
        {"$merge": {
            "into": prov_coll_name,
            "whenMatched": "replace",
            "whenNotMatched": "discard",
        }},
    ]
    # NB: $merge writes the cursor docs back into the same collection.
    # Server reads from a snapshot before the writes start; no infinite-loop risk.
    list(prov_coll.aggregate(pipeline, allowDiskUse=True))

    # We can't get a precise modified count from $merge directly; report the
    # set of NPIs that had a non-empty secondary list.
    summary = {
        "npis_with_secondary": len(npi_to_addrs),
        "rows_loaded": rows_loaded,
        "providers_targeted": len(npi_to_addrs),
        "lookup_collection": pl_lookup_collection,
        "version": version,
    }
    logging.info("attach_practice_locations: %s", summary)
    return summary


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
        })

    # NPPES header (~330 columns, ~7 KB JSON) is no longer embedded in each
    # partition dict — at 16+ workers the cumulative orchestrator outbox
    # exceeds the Durable 45 KB largemessages spill threshold and dispatch
    # stalls. Each worker fetches the header from the CSV blob at startup
    # (first 32 KB read, ~milliseconds).
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
    provider_collection = config.get(
        "provider_collection", "dev_PublicHealthData.providers"
    )
    db_name, coll_name = provider_collection.split(".", 1)
    collection = _get_mongo_client()[db_name][coll_name]
    # load_record_unique only needed for incremental retry idempotency.
    # Full loads have npi_unique as the natural key.
    if config.get("incremental", False):
        collection.create_index(
            [("load_id", 1), ("record_id", 1)],
            unique=True,
            name="load_record_unique",
        )
    collection.create_index(
        "addresses.zip",
        name="addresses_zip",
    )
    collection.create_index(
        "county.fips",
        name="county_fips",
    )
    collection.create_index(
        "worker_id",
        name="worker_id",
    )
    collection.create_index(
        "npi",
        unique=True,
        name="npi_unique",
    )
    collection.create_index(
        "addresses.state",
        name="addresses_state_idx",
    )
    collection.create_index(
        "taxonomies.code",
        name="taxonomy_code",
    )
    # F-105 proprietary flag indexes. The flag values are written in Step 13
    # (provider_flags_enrichment.apply_provider_flags_fn) which scans every
    # in-scope record after normalization and county enrichment complete.
    # Non-sparse so the index covers every doc.
    collection.create_index("can_prescribe", name="can_prescribe_idx")
    collection.create_index("is_homeopathic", name="is_homeopathic_idx")
    logging.info("Post-load indexes ensured on %s", provider_collection)


_STATUS_LABELS = {
    0:  "New",
    10: "Load invoked",
    11: "Load failed",
    12: "Load succeeded",
}


def _status_fields(code: int) -> dict:
    return {"status": code, "status_text": _STATUS_LABELS.get(code, str(code))}


def write_metadata_fn(config: dict) -> list:
    """Write one metadata record per worker. Returns list of inserted _id strings."""
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
            **_status_fields(0),
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
    """Drain the providers staging collection before loading new data.

    EPIC-010-F-006-S-001 / S-003 — state-scope semantics across the entire
    provider pipeline. Uses the shared state_filter helper so the predicate is
    100% uniform with load, enrichment, and embedding (REQ-T-001).

    Three modes:
      - incremental=True   -> no-op (defensive; orchestrator already skipped)
      - states == ["ALL"]  -> drop()  (full-load sentinel, REQ-B-002 / T-004)
      - states non-empty   -> delete_many() with the multi-state-bearing-field
                              predicate (REQ-T-003). Out-of-scope records survive.

    Raises ValueError if `states` is missing, empty, or malformed (REQ-T-002) —
    every step in the pipeline must raise on this; drain is no exception.

    drop() vs delete_many() trade-off: drop() returns disk space to Atlas
    immediately; delete_many() leaves WiredTiger holding the allocated space as
    fragmented free pages. The space cost is the price of preserving out-of-scope
    records when a load is state-scoped.

    Called after download/extract/partition succeed so we know the new data is viable.
    """
    from state_filter import normalize_states, is_full_load, mongo_state_filter

    provider_collection = config.get("provider_collection", "dev_PublicHealthData.providers")
    db_name, coll_name = provider_collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    if config.get("incremental", False):
        logging.info("drain_staging_fn: incremental=True — skipping drain on %s", provider_collection)
        return {"drained": False, "mode": "incremental_skip"}

    states = normalize_states(config)  # raises on missing/empty/malformed

    if is_full_load(states):
        coll.drop()
        logging.info("drain_staging_fn: full drop of %s (states=ALL) — disk space returned to Atlas.", provider_collection)
        return {"drained": True, "mode": "full_drop", "states": states}

    predicate = mongo_state_filter(states)
    result = coll.delete_many(predicate)
    logging.info(
        "drain_staging_fn: state-scoped delete on %s — states=%s deleted=%d (out-of-scope records preserved).",
        provider_collection, states, result.deleted_count,
    )
    return {"drained": True, "mode": "state_scoped", "deleted": result.deleted_count, "states": states}


def report_fn(config: dict) -> dict:
    from discrepancy_reporter import DiscrepancyReporter
    return DiscrepancyReporter().send(config)


_PROVIDER_REPORT_COLLECTION = "admin.PipelineDiscrepancyReports"


def embed_worker_fn(config: dict) -> dict:
    from embedding_worker import EmbeddingWorker
    config = {**config, "report_collection": config.get("report_collection", _PROVIDER_REPORT_COLLECTION)}
    return EmbeddingWorker(config).pipeline_execute()


def stamp_embedding_version_fn(config: dict) -> dict:
    """Backfill embedding_version and embedding_model onto already-embedded records
    that predate version stamping. Idempotent — skips records already stamped.
    """
    from county_enrichment_job import _build_states_filter
    from embedding_worker import EMBED_VERSION, EMBED_MODEL
    provider_collection = config.get("provider_collection", "dev_PublicHealthData.providers")
    db_name, coll_name = provider_collection.split(".", 1)
    collection = _get_mongo_client()[db_name][coll_name]

    sf = _build_states_filter(config)  # mandatory state filter
    result = collection.update_many(
        {"embedding": {"$exists": True}, "embedding_version": {"$exists": False}, **sf},
        {"$set": {"embedding_version": EMBED_VERSION, "embedding_model": EMBED_MODEL}},
    )
    logging.info(
        "StampEmbeddingVersion: %d records stamped with version=%s model=%s",
        result.modified_count, EMBED_VERSION, EMBED_MODEL,
    )
    return {
        "stamped": result.modified_count,
        "embedding_version": EMBED_VERSION,
        "embedding_model": EMBED_MODEL,
    }


def create_vector_index_fn(config: dict) -> dict:
    """Create Atlas Vector Search index on providers_staging. Idempotent."""
    provider_collection = config.get("provider_collection", "dev_PublicHealthData.providers")
    db_name, coll_name = provider_collection.split(".", 1)
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
                        "numDimensions": 3072,
                        "similarity": "cosine",
                    }
                ]
            },
        })
        logging.info("Vector search index '%s' created on %s", index_name, provider_collection)
    except Exception as exc:
        if "already exists" in str(exc).lower() or "duplicate" in str(exc).lower():
            logging.info("Vector search index '%s' already exists — skipping.", index_name)
        else:
            raise
    return {"index": index_name, "status": "ready"}



