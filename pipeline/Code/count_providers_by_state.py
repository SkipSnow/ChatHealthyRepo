# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Count providers by state from the authoritative blob source.
# Streams the NPPES CSV — never loads full file into memory.
#
# Called as a sync task via Router: {"ChatHealthyTask": "CountProvidersByState"}

import csv
import io
import logging
import os
from collections import Counter

_log = logging.getLogger("pipeline.count_providers")


def count_providers_by_state(payload: dict = None) -> dict:
    """Stream the NPPES CSV from blob and count providers per state.

    Returns: {"counts": {"DE": 26000, "MS": 54000, ...}, "total": 9000000}
    """
    from blob_client import get_blob_service, CONTAINER_PUBLIC_DATA

    blob_name = "provider/npi_fallback.csv"
    if payload and payload.get("blob_name"):
        blob_name = payload["blob_name"]

    _log.info("Counting providers by state from %s/%s", CONTAINER_PUBLIC_DATA, blob_name)

    bs = get_blob_service()
    blob = bs.get_blob_client(CONTAINER_PUBLIC_DATA, blob_name)
    downloader = blob.download_blob()

    state_counts = Counter()
    total = 0
    header = None
    state_idx = -1
    line_buffer = ""

    for chunk in downloader.chunks():
        text = line_buffer + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        line_buffer = lines[-1]

        for line in lines[:-1]:
            if header is None:
                # Parse CSV header to find state column
                reader = csv.reader(io.StringIO(line))
                header = next(reader)
                for i, col in enumerate(header):
                    if "Provider Business Practice Location Address State Name" in col:
                        state_idx = i
                        break
                if state_idx < 0:
                    # Fallback: any column with 'state' in name
                    for i, col in enumerate(header):
                        if "state" in col.lower() and "practice" in col.lower():
                            state_idx = i
                            break
                _log.info("State column: index=%d, name=%s", state_idx, header[state_idx] if state_idx >= 0 else "NOT FOUND")
                continue

            total += 1
            if state_idx >= 0:
                reader = csv.reader(io.StringIO(line))
                try:
                    fields = next(reader)
                    if len(fields) > state_idx:
                        state = fields[state_idx].strip()
                        if state and len(state) == 2:
                            state_counts[state] += 1
                except StopIteration:
                    pass

            if total % 1000000 == 0:
                _log.info("Scanned %d rows", total)

    result = {
        "counts": dict(state_counts.most_common()),
        "total": total,
        "target_states": {s: state_counts.get(s, 0) for s in ["DE", "MS", "VA", "IN"]},
    }
    _log.info("Done: %d total, target states: %s", total, result["target_states"])
    return result
