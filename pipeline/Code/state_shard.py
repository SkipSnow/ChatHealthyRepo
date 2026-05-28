# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""state_shard - split the NPPES source CSV into 51 per-practice-state shards.

One sequential pass over the master CSV partitions rows by
`Provider Business Practice Location Address State Name`. Each US state +
DC + an "OTHER" bucket (territories / foreign / blank) gets its own output
blob containing the header followed by all rows whose practice address
state matches.

The pipeline then runs against a single shard blob with NO state filter.
Every record in the shard is by definition a hit, so the activity's
per-row filter overhead and chunk-fixed parse-then-discard cost disappear.

Output layout:
  state_shards/{source_blob_name}.{STATE}.csv          (51 shards)
  state_shards/{source_blob_name}.shard_manifest.json  (sidecar)

The manifest is keyed by uppercase 2-letter state code (plus "OTHER"):
  { "DE": {"blob_path": "...", "row_count": 89000, "file_size": 12345},
    "CA": {...}, ..., "OTHER": {...},
    "source_size": <bytes>, "header_end": <int>, "built_at": "..." }

If the manifest exists and references the same source_size, the split
is skipped — cached. NPPES is dissemination data; the same source file
shards identically every run.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import uuid
from datetime import datetime, timezone

from blob_client import get_blob_service


_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","DC","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ","NM",
    "NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT","VA","WA",
    "WV","WI","WY",
}

_PRACTICE_STATE_COL = "Provider Business Practice Location Address State Name"
_BLOCK_FLUSH_BYTES = 8 * 1024 * 1024  # 8 MB per staged block


def _shard_blob_name(prefix: str, source_blob_name: str, state: str) -> str:
    return f"{prefix}{source_blob_name}.{state}.csv"


def _manifest_blob_name(prefix: str, source_blob_name: str) -> str:
    return f"{prefix}{source_blob_name}.shard_manifest.json"


def build_state_shards(
    container: str,
    source_blob_name: str,
    output_prefix: str = "state_shards/",
) -> dict:
    """Stream the source CSV once and emit 51 per-state shard blobs plus
    a manifest. Returns the manifest dict. Idempotent via size-key cache."""
    svc = get_blob_service()
    cc = svc.get_container_client(container)
    src_client = cc.get_blob_client(source_blob_name)
    src_props = src_client.get_blob_properties()
    source_size = src_props.size

    # Cache check
    manifest_path = _manifest_blob_name(output_prefix, source_blob_name)
    manifest_client = cc.get_blob_client(manifest_path)
    try:
        if manifest_client.exists():
            cached = json.loads(manifest_client.download_blob().readall())
            if cached.get("source_size") == source_size:
                logging.info("state_shard: cache hit %s", manifest_path)
                return cached
            logging.info("state_shard: cache mismatch — rebuilding")
    except Exception as exc:
        logging.warning("state_shard: cache read failed (%s); rebuilding", exc)

    # Stream source, splitting per state
    download = src_client.download_blob()
    # Detect the header line and the practice-state column index
    header_buffer = bytearray()
    chunks_iter = download.chunks()
    block = next(chunks_iter)
    header_buffer.extend(block)
    nl = header_buffer.find(b"\n")
    if nl < 0:
        raise RuntimeError("state_shard: source header not found in first chunk")
    header_bytes = bytes(header_buffer[: nl + 1])  # includes trailing \n
    header_end = nl + 1
    header_text = header_bytes.decode("utf-8", errors="replace").rstrip("\r\n")
    header_cols = next(csv.reader([header_text]))
    try:
        state_idx = header_cols.index(_PRACTICE_STATE_COL)
    except ValueError as exc:
        raise RuntimeError(
            f"state_shard: header missing column {_PRACTICE_STATE_COL!r}"
        ) from exc

    # Per-state writers: bytearray buffers, block IDs, row counters
    state_buffer: dict[str, bytearray] = {}
    state_blocks: dict[str, list[str]] = {}
    state_count: dict[str, int] = {}
    state_size: dict[str, int] = {}
    state_clients: dict[str, object] = {}

    def _ensure_state(s: str):
        if s in state_buffer:
            return
        state_buffer[s] = bytearray()
        state_buffer[s].extend(header_bytes)  # every shard starts with the header
        state_blocks[s] = []
        state_count[s] = 0
        state_size[s] = 0
        state_clients[s] = cc.get_blob_client(
            _shard_blob_name(output_prefix, source_blob_name, s)
        )

    def _flush(s: str, force: bool = False):
        buf = state_buffer[s]
        if not buf:
            return
        if not force and len(buf) < _BLOCK_FLUSH_BYTES:
            return
        block_id = uuid.uuid4().hex
        # Azure block IDs must be base64-encoded same-length strings
        import base64
        block_id_b64 = base64.b64encode(block_id.encode("ascii")).decode("ascii")
        state_clients[s].stage_block(block_id_b64, bytes(buf))
        state_blocks[s].append(block_id_b64)
        state_size[s] += len(buf)
        buf.clear()

    # Process body: header consumed, now stream rows
    leftover = bytearray(header_buffer[header_end:])
    header_buffer.clear()

    def _process_row_bytes(row_line: bytes):
        # row_line is the raw line WITHOUT trailing \n
        if not row_line:
            return
        line_text = row_line.decode("utf-8", errors="replace")
        row = next(csv.reader([line_text]), None)
        if not row or len(row) <= state_idx:
            return
        s = (row[state_idx] or "").strip().upper()
        if s not in _US_STATES:
            s = "OTHER"
        _ensure_state(s)
        state_buffer[s].extend(row_line)
        state_buffer[s].extend(b"\n")
        state_count[s] += 1
        _flush(s, force=False)

    # Consume the rest of the first downloaded chunk
    while True:
        nl = leftover.find(b"\n")
        if nl < 0:
            break
        _process_row_bytes(bytes(leftover[:nl]))
        del leftover[: nl + 1]

    # Continue streaming
    for block in chunks_iter:
        leftover.extend(block)
        while True:
            nl = leftover.find(b"\n")
            if nl < 0:
                break
            _process_row_bytes(bytes(leftover[:nl]))
            del leftover[: nl + 1]

    if leftover:
        _process_row_bytes(bytes(leftover))

    # Final flush + commit_block_list for each state
    for s in list(state_buffer.keys()):
        _flush(s, force=True)
        state_clients[s].commit_block_list(state_blocks[s])

    # Manifest
    manifest = {
        "source_blob_name": source_blob_name,
        "source_size": source_size,
        "header_end": header_end,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "states": {
            s: {
                "blob_path": _shard_blob_name(output_prefix, source_blob_name, s),
                "row_count": state_count[s],
                "file_size": state_size[s] + len(header_bytes),
            }
            for s in state_buffer
        },
    }
    manifest_bytes = json.dumps(manifest, indent=2).encode("utf-8")
    manifest_client.upload_blob(manifest_bytes, overwrite=True)
    logging.info(
        "state_shard: built %d shards from %s (%d bytes) -> %s",
        len(state_buffer), source_blob_name, source_size, manifest_path,
    )
    return manifest


def build_state_shards_activity_fn(config: dict) -> dict:
    """Durable activity entry point."""
    container = config.get("blob_container", "provider-data")
    source_blob_name = config["csv_path"]
    output_prefix = config.get("shard_prefix", "state_shards/")
    manifest = build_state_shards(
        container=container,
        source_blob_name=source_blob_name,
        output_prefix=output_prefix,
    )
    return manifest
