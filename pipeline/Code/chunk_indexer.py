# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""chunk_indexer - precise record-boundary chunking for the source CSV.

One-pass scan over the source CSV that emits chunks aligned to newline
boundaries. Each chunk's [start_byte, end_byte) holds exactly N complete
records (configurable). Workers can stream their range with zero boundary
fix-up.

The index is persisted alongside the CSV as a sidecar blob:
  {csv_blob_name}.chunk_index.json

If the sidecar exists and references the same (file_size, batch_size), the
scan is skipped and the cached index is returned.

NPPES dissemination CSV does not include embedded newlines in quoted
fields — every \\n marks end-of-record. The naive byte-level scan is safe.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import json

import os

from blob_client import get_blob_service

_READ_BUFFER_BYTES = 8 * 1024 * 1024  # 8 MB per stream read


def _index_blob_name(csv_blob_name: str) -> str:
    return f"{csv_blob_name}.chunk_index.json"


def build_chunk_index(
    container: str,
    csv_blob_name: str,
    file_size: int,
    header_end: int,
    batch_size: int,
) -> list[list[int]]:
    """Return list of [start_byte, end_byte] chunks. Each chunk covers
    exactly batch_size complete records (the final chunk may have fewer).

    Side effect: writes / reads the sidecar index blob. The cache key
    includes (file_size, batch_size, header_end) so changes invalidate it.
    """
    svc = get_blob_service()
    cc = svc.get_container_client(container)
    idx_blob = cc.get_blob_client(_index_blob_name(csv_blob_name))

    # Try cache
    try:
        if idx_blob.exists():
            cached_bytes = idx_blob.download_blob().readall()
            cached = json.loads(cached_bytes)
            if (
                cached.get("file_size") == file_size
                and cached.get("batch_size") == batch_size
                and cached.get("header_end") == header_end
            ):
                ChatHealthyLoggingService().info(
                    "chunk_indexer: cache hit %s (%d chunks)",
                    _index_blob_name(csv_blob_name), len(cached.get("chunks") or []),
                )
                return cached["chunks"]
            ChatHealthyLoggingService().info("chunk_indexer: cache mismatch — rebuilding")
    except Exception as exc:
        ChatHealthyLoggingService().warning("chunk_indexer: cache read failed (%s) — rebuilding", exc)

    # Fresh scan
    csv_blob = cc.get_blob_client(csv_blob_name)
    chunks: list[list[int]] = []
    chunk_start = header_end
    records_in_chunk = 0
    pos = header_end

    stream = csv_blob.download_blob(offset=header_end).chunks()
    leftover = bytearray()
    for block in stream:
        # Combine with leftover, scan for newlines, emit chunks
        if leftover:
            block = bytes(leftover) + block
            leftover.clear()
        last_nl_end = 0  # offset in `block` past the last \n we processed
        i = 0
        while i < len(block):
            nl = block.find(b"\n", i)
            if nl < 0:
                # No more newlines in this block — buffer the tail
                leftover.extend(block[last_nl_end:])
                break
            # Newline at position nl. Absolute byte position past this newline:
            pos += (nl + 1 - i)
            i = nl + 1
            last_nl_end = nl + 1
            records_in_chunk += 1
            if records_in_chunk >= batch_size:
                chunks.append([chunk_start, pos])
                chunk_start = pos
                records_in_chunk = 0
        else:
            # Fully scanned without break — but we may have trailing bytes
            if last_nl_end < len(block):
                leftover.extend(block[last_nl_end:])

    # Final partial chunk: trailing bytes without newline still count
    if leftover or records_in_chunk > 0:
        # Don't extend pos past file_size
        chunks.append([chunk_start, file_size])

    # Persist sidecar
    payload = json.dumps({
        "file_size": file_size,
        "header_end": header_end,
        "batch_size": batch_size,
        "total_chunks": len(chunks),
        "chunks": chunks,
    }).encode("utf-8")
    try:
        idx_blob.upload_blob(payload, overwrite=True)
        ChatHealthyLoggingService().info(
            "chunk_indexer: wrote sidecar %s (%d chunks)",
            _index_blob_name(csv_blob_name), len(chunks),
        )
    except Exception as exc:
        ChatHealthyLoggingService().warning("chunk_indexer: sidecar write failed: %s", exc)

    return chunks


def build_chunk_index_activity_fn(config: dict) -> dict:
    """Durable activity entry point."""
    container = config.get("blob_container", "provider-data")
    csv_blob_name = config["csv_path"]
    file_size = int(config["file_size"])
    header_end = int(config["header_end"])
    batch_size = int(config.get("batch_size", 1000))
    chunks = build_chunk_index(
        container=container,
        csv_blob_name=csv_blob_name,
        file_size=file_size,
        header_end=header_end,
        batch_size=batch_size,
    )
    return {"total_chunks": len(chunks), "chunks": chunks}
