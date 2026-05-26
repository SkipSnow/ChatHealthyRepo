# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""streaming_pipeline — records-as-messages straight-through architecture.

REPLACES the store-and-forward-via-Mongo model (Step 4 raw load → Step 5
normalize → Step 7 Pass 1 → ... → each step reading and rewriting every doc).

Each NPI flows through the stages as one PipelineMessage object, inline,
inside a single per-partition activity. The activity:

    1. Streams its CSV byte-range
    2. For each row, applies the raw state filter
    3. Builds a PipelineMessage and runs it through every in-memory stage:
         - normalize_stage      → addresses[] + active event log
         - pass1_zip_stage      → addresses[*].county.fips from ZIP crosswalk
    4. Bulk-writes the accumulated PipelineMessage.to_mongo_doc() batch
       to Mongo ONCE per batch_size records.

No intermediate Mongo round-trips. No long-lived cursors across stages.
Per-partition pipelining → independent throughput, no global degradation.

Out of scope for THIS file (spike Phase 1):
    - Pass 2 (Census Geocoder) — needs network calls; deferred
    - Pass 3 (Billing geocoder) — needs network calls; deferred
    - Pass 4 (Google Maps) — needs network calls; deferred
    - Pass 6 (NPPES API) — needs network calls; deferred
    - Multi-practice-address attach (pl_pfile)
    - Urban flag (RUCC)
    - Provider flags (proprietary catalog)
    - Embedding (OpenAI)

Each can be added as its own in-memory stage function once the spike
validates flat-line throughput at a steady-state rate.

Schema contract (Website/schemas/ChatHealthyProvidersSchema.json) is what
the final to_mongo_doc() must satisfy. Stages mutate `content` toward that
shape and Mongo-write is the conformance boundary.
"""
from __future__ import annotations

import csv
import io
import logging
import os
import time
from datetime import datetime, timezone, timedelta

from pymongo import MongoClient, UpdateOne


# ── Mongo client (process-wide) ─────────────────────────────────────────────

_mongo: MongoClient | None = None


def _get_mongo_client() -> MongoClient:
    global _mongo
    if _mongo is None:
        _mongo = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
            maxPoolSize=64,
        )
    return _mongo


# ── Stage functions (pure — take PipelineMessage, mutate it) ───────────────

def normalize_stage(msg) -> None:
    """Raw NPPES dict → addresses[] + active event log + identity fields.

    Delegates to normalize_raw_record (the same pure normalizer the old
    Step 5 batch worker uses) so the schema-conformant output shape is
    identical regardless of which pipeline produced it.
    """
    from normalize_provider_rows import normalize_raw_record
    msg.enter_stage("normalize")
    if not msg.raw:
        msg.exit_stage("normalize")
        return
    normalized = normalize_raw_record(msg.raw)
    # Preserve identity fields the normalizer doesn't carry, then layer
    # the normalized output on top so addresses[], active[], taxonomies,
    # etc. populate content.
    msg.content.update(normalized)
    # Carry over the NPPES top-level columns the normalizer skipped
    # (entity_type_code, provider_*_name, dates, organization fields, etc.)
    # via the identical pass-through list provider_worker uses for the
    # legacy path. Keeping the schema-conformant boundary intact.
    _layer_passthrough_fields(msg.raw, msg.content)
    msg.raw = None  # release memory; raw payload no longer needed
    msg.exit_stage("normalize")


# NPPES top-level columns that the normalizer does not transform — the
# legacy worker.py passes them through verbatim. Mirror that list here so
# the streaming output matches the schema's required identity fields.
_PASSTHROUGH_RAW_COLUMNS = {
    "NPI": "npi",
    "Entity Type Code": "entity_type_code",
    "Provider Name Prefix Text": "provider_name_prefix_text",
    "Provider First Name": "provider_first_name",
    "Provider Middle Name": "provider_middle_name",
    "Provider Last Name (Legal Name)": "provider_last_name_legal_name",
    "Provider Name Suffix Text": "provider_name_suffix_text",
    "Provider Credential Text": "provider_credential_text",
    "Provider Other Organization Name": "provider_other_organization_name",
    "Provider Other Organization Name Type Code": "provider_other_organization_name_type_code",
    "Provider Organization Name (Legal Business Name)": "provider_organization_name_legal_business_name",
    "Provider Sex Code": "provider_sex_code",
    "Is Sole Proprietor": "is_sole_proprietor",
    "Is Organization Subpart": "is_organization_subpart",
    "Parent Organization LBN": "parent_organization_lbn",
    "Parent Organization TIN": "parent_organization_tin",
    "Authorized Official Last Name": "authorized_official_last_name",
    "Authorized Official First Name": "authorized_official_first_name",
    "Authorized Official Middle Name": "authorized_official_middle_name",
    "Authorized Official Title or Position": "authorized_official_title_or_position",
    "Authorized Official Credential Text": "authorized_official_credential_text",
    "Employer Identification Number (EIN)": "employer_identification_number_ein",
    "Provider Enumeration Date": "provider_enumeration_date",
    "Last Update Date": "last_update_date",
    "Certification Date": "certification_date",
}


def _layer_passthrough_fields(raw: dict, content: dict) -> None:
    """Copy NPPES top-level identity fields the normalizer doesn't touch
    onto the schema-conformant content dict. Empty values omitted."""
    for raw_col, doc_field in _PASSTHROUGH_RAW_COLUMNS.items():
        v = (raw.get(raw_col) or "").strip()
        if v:
            content[doc_field] = v


def pass1_zip_stage(msg, crosswalk: dict) -> None:
    """Apply ZIP crosswalk to every addresses[] entry whose ZIP resolves
    unambiguously (res_ratio >= 0.98). Sets county.fips, county.name,
    county.source='crosswalk_pass1'. Ambiguous/split ZIPs left for later
    stages (deferred for spike).
    """
    msg.enter_stage("pass1_zip")
    for addr in msg.content.get("addresses") or []:
        if not isinstance(addr, dict):
            continue
        z = (addr.get("zip") or "")[:5]
        if not z:
            continue
        match = crosswalk.get(z)
        if not match or match.get("is_split"):
            continue
        ratio = match.get("ratio") or 0
        if ratio < 0.98:
            continue
        addr["county"] = {
            "fips":      match["fips"],
            "name":      match.get("name") or "",
            "source":    "crosswalk_pass1",
            "zip_ratio": ratio,
        }
    msg.exit_stage("pass1_zip")


# ── Raw row → state filter (mirror of provider_worker._raw_row_matches_state) ─

_PRACTICE_STATE_COL = "Provider Business Practice Location Address State Name"
_MAILING_STATE_COL  = "Provider Business Mailing Address State Name"
_LICENSE_STATE_PREFIX = "Provider License Number State Code_"
_OID_STATE_PREFIX     = "Other Provider Identifier State_"


def _raw_row_matches_state(raw_row: dict, states_set: set) -> bool:
    for col in (_PRACTICE_STATE_COL, _MAILING_STATE_COL):
        v = (raw_row.get(col) or "").strip().upper()
        if v in states_set:
            return True
    for k, v in raw_row.items():
        if not v:
            continue
        if k.startswith(_LICENSE_STATE_PREFIX) or k.startswith(_OID_STATE_PREFIX):
            if v.strip().upper() in states_set:
                return True
    return False


# ── CSV byte-range streamer (mirror of provider_worker pattern) ────────────

def _fetch_header(blob_client) -> list:
    """Fetch + parse the CSV header from the first 32KB of the blob.

    Mirrors partition_file_fn's header-extraction path. Each worker fetches
    its own header rather than receiving it through orchestrator config —
    at 16+ workers the cumulative outbox spills the Durable 45 KB
    largemessages threshold.
    """
    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    if "\n" not in header_text:
        raise RuntimeError("Header line exceeds 32KB — unexpected CSV format.")
    header_line = header_text.split("\n")[0]
    return list(csv.reader([header_line]))[0]


def _iter_csv_partition(blob_client, start_byte: int, end_byte: int, header: list):
    """Yield (record_id, row_dict) for each CSV row in the byte range.

    Streams the blob in chunks and emits one decoded line at a time — never
    holds the full partition in memory. Identical pattern to
    provider_worker._iter_lines. Required because a single worker on a
    100-partition split of an 8 GB CSV would otherwise hold ~80 MB of bytes +
    ~80 MB of decoded text + StringIO buffer, blowing the FA per-worker memory
    ceiling (OOM-kill / SIGKILL / exit 137).

    Same partition contract as provider_worker:
      - start_byte > 0 lands mid-line; drop the leading partial line —
        that's the preceding worker's territory.
      - Stop at end_byte (length of the download).
    """
    stream = blob_client.download_blob(offset=start_byte, length=end_byte - start_byte)
    local_id = 0
    buffer = b""
    expected_field_count = len(header)

    for chunk in stream.chunks():
        buffer += chunk
        while b"\n" in buffer:
            line_bytes, buffer = buffer.split(b"\n", 1)
            line = line_bytes.decode("utf-8", errors="replace")
            if not line:
                continue
            row = next(csv.reader([line]), None)
            # Defensive: drop rows with wrong field count. This catches a
            # partial leading row from boundary misalignment (rare; partition
            # boundaries are pre-aligned to \n by partition_file_fn but the
            # check is cheap insurance), and trailing rows that don't have
            # a terminating \n in this partition (next worker's territory).
            if not row or len(row) != expected_field_count:
                continue
            local_id += 1
            yield local_id, dict(zip(header, row))
    # Any bytes left in `buffer` (no terminating \n in this partition) are
    # the next worker's territory — drop them.


# ── Activity: get_csv_metadata (file_size + header_end for seed) ───────────

def get_csv_metadata_fn(config: dict) -> dict:
    """Read the CSV blob's file_size and header_end (byte offset after the
    header line). Cheap blob op (32 KB peek + 1 properties call) — fast
    enough to run as a single activity at orchestrator startup."""
    from blob_client import get_blob_service

    csv_path = config["csv_path"]
    container = config.get("blob_container", "provider-data")
    blob_client = get_blob_service().get_container_client(container).get_blob_client(csv_path)
    props = blob_client.get_blob_properties()
    file_size = props.size

    header_bytes = blob_client.download_blob(offset=0, length=32768).readall()
    header_text = header_bytes.decode("utf-8", errors="replace")
    if "\n" not in header_text:
        raise RuntimeError("Header line exceeds 32KB — unexpected CSV format.")
    header_line = header_text.split("\n")[0]
    header_end = len(header_line.encode("utf-8")) + 1  # +1 for \n
    logging.info("csv_metadata: file_size=%d header_end=%d", file_size, header_end)
    return {"file_size": file_size, "header_end": header_end}


# ── Activity: streaming_partition_activity (pull-loop worker) ──────────────

def streaming_partition_activity_fn(config: dict) -> dict:
    """Pull-loop worker. Claims chunks from task_manager continuously,
    processes each inline, releases, repeats. Exits when queue is empty.

    Records-as-messages: the worker (this activity) "sends a message" to the
    task_manager entity asking for the next 5K assignments. The entity
    atomically pops a chunk from the queue; the worker processes it
    straight-through (parse + normalize + pass1 + bulk-write) and signals
    release before pulling the next.

    Activity-runtime budget: stops claiming new chunks if we approach the
    Durable 2 h activity timeout. Any in-flight chunk that doesn't complete
    is left in task_manager.in_flight; a follow-up reaper or fresh run can
    re-claim it (Mongo upsert is idempotent on NPI so re-processing is
    safe). Spike scope: no reaper yet; relies on the budget margin.
    """
    from pipeline_message import PipelineMessage
    from county_enrichment_job import _get_crosswalk
    from blob_client import get_blob_service
    from task_manager import claim_next_chunk, release_chunk

    started_at  = datetime.now(timezone.utc).isoformat()
    start_time  = time.monotonic()
    worker_id   = str(config["worker_id"])
    csv_path    = config["csv_path"]
    load_id     = config["load_id"]
    states      = [s.upper() for s in config["states"]]
    states_set  = set(states)
    batch_size  = config.get("batch_size", 500)
    container   = config.get("blob_container", "provider-data")
    collection  = config.get("provider_collection", "dev_PublicHealthData.pipeline_providers")
    # Stop claiming new chunks when we have less than this much headroom
    # before the 7200 s Durable activity hard timeout. Default 600 s = 10 min.
    safety_margin = config.get("activity_safety_margin_seconds", 600)
    max_runtime   = 7200 - safety_margin

    db_name, coll_name = collection.split(".", 1)
    coll = _get_mongo_client()[db_name][coll_name]

    blob_client = get_blob_service().get_container_client(container).get_blob_client(csv_path)
    header = _fetch_header(blob_client)
    crosswalk = _get_crosswalk()

    chunks_processed = 0
    total_seen = total_filtered = total_upserted = 0

    while True:
        if (time.monotonic() - start_time) > max_runtime:
            logging.info(
                "worker[%s]: hit %ds runtime ceiling — returning before next claim",
                worker_id, max_runtime,
            )
            break

        chunk = claim_next_chunk(worker_id)
        if chunk is None:
            # task_manager queue is empty; worker's job is done.
            logging.info("worker[%s]: queue drained — exiting", worker_id)
            break

        chunk_id   = chunk["chunk_id"]
        start_byte = chunk["start_byte"]
        end_byte   = chunk["end_byte"]
        chunk_t0   = time.monotonic()

        buffer: list = []
        seen = filtered = 0

        def _flush():
            nonlocal total_upserted
            if not buffer:
                return
            ops = [
                UpdateOne({"npi": doc["npi"]}, {"$set": doc}, upsert=True)
                for doc in buffer
            ]
            result = coll.bulk_write(ops, ordered=False)
            total_upserted += (result.upserted_count + result.modified_count + result.matched_count)
            buffer.clear()

        for local_id, raw_row in _iter_csv_partition(blob_client, start_byte, end_byte, header):
            seen += 1
            if not _raw_row_matches_state(raw_row, states_set):
                filtered += 1
                continue
            npi = (raw_row.get("NPI") or "").strip()
            if not npi:
                continue

            msg = PipelineMessage.from_raw_row(
                npi=npi,
                raw_row=raw_row,
                load_id=load_id,
                record_id=local_id,
                worker_id=int(worker_id) if worker_id.isdigit() else 0,
                partition=(start_byte, end_byte),
            )
            normalize_stage(msg)
            pass1_zip_stage(msg, crosswalk)
            buffer.append(msg.to_mongo_doc())
            if len(buffer) >= batch_size:
                _flush()

        _flush()
        release_chunk(chunk_id)

        chunks_processed += 1
        total_seen += seen
        total_filtered += filtered
        chunk_dur = time.monotonic() - chunk_t0
        logging.info(
            "worker[%s] chunk %d done: seen=%d filtered=%d upserted=%d "
            "chunk_dur=%.1fs (chunks=%d)",
            worker_id, chunk_id, seen, filtered, total_upserted, chunk_dur, chunks_processed,
        )

    duration = round(time.monotonic() - start_time, 2)
    rate = round(total_upserted / duration, 1) if duration > 0 else 0.0
    logging.info(
        "worker[%s] DONE: chunks=%d seen=%d filtered=%d upserted=%d duration=%.1fs rate=%.1f/s",
        worker_id, chunks_processed, total_seen, total_filtered, total_upserted, duration, rate,
    )
    return {
        "worker_id":        worker_id,
        "chunks_processed": chunks_processed,
        "seen":             total_seen,
        "filtered_out":     total_filtered,
        "upserted":         total_upserted,
        "started_at":       started_at,
        "finished_at":      datetime.now(timezone.utc).isoformat(),
        "duration":         duration,
        "rate_per_sec":     rate,
    }


# ── Orchestrator: streaming_pipeline_orchestrator ──────────────────────────

def streaming_pipeline_orchestrator_fn(context):
    """Parent orchestrator for the records-as-messages spike.

    Reserves the cluster, prepares the NPPES file (download + extract +
    compute byte partitions), fans out streaming_partition_activity for
    every partition in parallel, rolls up the per-partition stats, and
    releases the reservation.

    Skips the legacy Step 4 (raw load) + Step 5 (normalize) + Step 7
    (Pass 1) chain entirely — those are subsumed by the per-partition
    activity's inline stage execution.

    Subsequent enrichment stages (Pass 2/3/4/6, urban flag, provider
    flags, embedding) are NOT wired in this spike; the orchestrator
    returns immediately after the streaming partitions complete.
    """
    import azure.durable_functions as df

    config = context.get_input() or {}
    load_id = context.instance_id
    config = {**config, "load_id": load_id}

    # Throttle preamble — fire-and-forget signal to each entity. We do
    # NOT yield on call_entity because Netherite's request/response message
    # delivery between orchestrator and entity is unreliable on this hub
    # (responses get dropped mid-flight, wedging the orchestrator on the
    # call_entity yield forever — observed at instance cee585e7 and
    # 2b234d35, both hung at 1/3 EventRaised even though entity peek showed
    # all 3 buckets correctly configured). signal_entity is one-way; the
    # entity processes the operation, no response is expected.
    #
    # Safety: configure() is idempotent on the entity side. Activities that
    # later call acquire() must tolerate False (unconfigured bucket) and
    # back off — covered by token_bucket_entity_fn returning False until
    # configured.
    throttle = config["throttle"]
    for b in ("nppes", "google_maps", "openai"):
        context.signal_entity(
            df.EntityId("token_bucket", b),
            "configure",
            throttle[b],
        )

    # Health + zombie kill (mandatory for every parent orchestrator).
    context.set_custom_status("Step 1: Health check + kill THIS pipeline's zombies")
    yield context.call_sub_orchestrator(
        "pipeline_health_and_zombie_kill_orchestrator",
        {
            "orchestrator_name": "streaming_pipeline_orchestrator",
            "current_load_id":   load_id,
            "load_id":           load_id,
            "states":            config["states"],
        },
    )

    # Cluster reserve (parity with ProviderPipelineOrchestrator).
    context.set_custom_status("Step 2: Reserving cluster")
    reservation = yield context.call_activity("wake_cluster_activity", {
        "cluster_name":              config["pipeline_cluster"],
        "job_id":                    load_id,
        "requester":                 "streaming_pipeline",
        "expected_duration_minutes": config["expected_duration_minutes"],
    })
    yield context.call_activity("check_cluster_state_activity", {
        "cluster_name": config["pipeline_cluster"],
    })
    yield context.call_activity("register_reservation_activity", {
        "cluster_name":              config["pipeline_cluster"],
        "job_id":                    load_id,
        "requester":                 "streaming_pipeline",
        "expected_duration_minutes": config["expected_duration_minutes"],
    })

    pipeline_error = None
    summary: dict = {}
    try:
        # Prepare data — reuse the existing prepare_data_orchestrator so
        # CSV download + partition computation is identical to legacy.
        # That orchestrator returns {csv_path, partitions, version, ...}.
        context.set_custom_status("Step 3: Prepare data (download + extract + partition)")
        prepare_result = yield context.call_sub_orchestrator(
            "prepare_data_orchestrator",
            {
                "load_id":             load_id,
                "num_workers":         config["num_workers"],
                "blob_container":      config["blob_container"],
                "states":              config["states"],
                "incremental":         False,
                "provider_collection": config["provider_collection"],
                "metadata_collection": config["metadata_collection"],
            },
        )

        # ── Records-as-messages pipeline: seed task_manager + spawn workers ──
        # The task_manager Durable Entity owns the queue of 5K-record byte
        # chunks. Workers (streaming_partition_activity) loop continuously:
        # claim a chunk via HTTP entity signal, process inline, release,
        # claim the next. Continuous flow, no fan-out waves with idle gaps.
        csv_path = prepare_result["csv_path"]

        # Step 4a: read file_size + header_end so we can seed the task_manager.
        context.set_custom_status("Step 4a: Reading CSV metadata for task_manager seed")
        meta = yield context.call_activity("get_csv_metadata_activity", {
            "csv_path":       csv_path,
            "blob_container": config["blob_container"],
        })

        # Step 4b: seed the task_manager entity. signal_entity is one-way;
        # the seed completes asynchronously inside the entity. Workers below
        # will loop their claim until the seed lands and chunks become
        # available (claim against an unseeded queue returns None, which
        # would make workers exit prematurely — so we add a small synchronous
        # warm-up wait via a Durable timer to give seed time to commit).
        chunk_size_bytes = int(config.get("chunk_size_bytes", 2_500_000))
        context.signal_entity(
            df.EntityId("task_manager", "default"),
            "seed",
            {
                "file_size":        meta["file_size"],
                "header_end":       meta["header_end"],
                "chunk_size_bytes": chunk_size_bytes,
            },
        )
        # Give the entity a few seconds to process the seed before workers
        # start claiming. Without this, race-against-empty-queue is possible.
        seed_settle_seconds = int(config.get("seed_settle_seconds", 5))
        yield context.create_timer(
            context.current_utc_datetime + timedelta(seconds=seed_settle_seconds)
        )

        # Step 4c: warm the FA + spawn N stateless pull-loop workers via the
        # shared fan_out_workers substrate. Each worker config has NO byte
        # range — the worker pulls from task_manager continuously until
        # the queue is drained.
        num_workers = int(config["num_workers"])
        worker_configs = [
            {
                "worker_id":           i + 1,
                "csv_path":            csv_path,
                "load_id":             load_id,
                "states":              config["states"],
                "batch_size":          config["batch_size"],
                "blob_container":      config["blob_container"],
                "provider_collection": config["provider_collection"],
            }
            for i in range(num_workers)
        ]
        context.set_custom_status(
            f"Step 4: Spawning {num_workers} pull-loop worker(s); "
            f"~{meta['file_size'] // chunk_size_bytes} chunks queued"
        )
        fan_out_result = yield context.call_sub_orchestrator(
            "fan_out_workers_orchestrator",
            {
                "worker_activity": "streaming_partition_activity",
                "worker_configs":  worker_configs,
                "num_workers":     num_workers,
                "step_label":      "Step 4: Streaming pull-loop workers",
            },
        )
        worker_results = fan_out_result.get("results") or []

        # Roll-up
        summary = {
            "workers_completed":  len(worker_results),
            "total_chunks":       sum(r.get("chunks_processed", 0) for r in worker_results),
            "rows_seen":          sum(r.get("seen", 0) for r in worker_results),
            "filtered_out":       sum(r.get("filtered_out", 0) for r in worker_results),
            "upserted":           sum(r.get("upserted", 0) for r in worker_results),
            "min_rate_per_sec":   min((r.get("rate_per_sec", 0) for r in worker_results), default=0),
            "max_rate_per_sec":   max((r.get("rate_per_sec", 0) for r in worker_results), default=0),
            "worker_results":     worker_results,
            "warm_metrics":       fan_out_result.get("warm_metrics"),
            "cool_metrics":       fan_out_result.get("cool_metrics"),
            "csv_file_size":      meta["file_size"],
            "chunk_size_bytes":   chunk_size_bytes,
        }
        context.set_custom_status(
            f"Done — upserted {summary['upserted']:,} records across "
            f"{summary['total_chunks']} chunks by {summary['workers_completed']} workers"
        )

    except Exception as exc:
        # No-fallback rule: surface, don't catch-and-swallow. Capture for
        # the operator-visible output and re-raise so the orchestrator
        # marks Failed and the reservation release in the finally runs.
        pipeline_error = {
            "type":    type(exc).__name__,
            "message": str(exc)[:500],
        }
        raise
    finally:
        # Always release the reservation.
        yield context.call_activity("release_reservation_activity", {
            "job_id": load_id,
        })

    return {
        "load_id":  load_id,
        "summary":  summary,
        "error":    pipeline_error,
    }
