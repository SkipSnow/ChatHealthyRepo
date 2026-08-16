# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Attach pl_pfile secondary practice addresses — LLD §4.9.

Per-state fanout. Each worker owns one business_address_state.
Streaming, chunked algorithm designed to scale from VT (~5K
providers) to CA (~400K providers) without holding all pl_pfile
matches or all provider docs in memory:

  1. Cursor-iterate Provider_v_3 filtered to this state's business
     address. Accumulate 1000 provider docs at a time into a chunk.
  2. For each chunk, extract the NPI list and query pl_pfile once
     with `$in` on those 1000 NPIs — small query payload, uses the
     NPI index on the pl_pfile staging collection, returns only the
     matching subset.
  3. Group returned pl_pfile rows by NPI in a per-chunk dict.
  4. For each provider in the chunk: merge and dedupe addresses in
     memory, push an UpdateOne into a bulk-op buffer.
  5. Flush the bulk-op buffer via bulk_write every 1000 ops.
  6. Discard the chunk's in-memory state; advance the provider cursor.

Bounded memory per worker: ~1000 provider docs + their matched
pl_pfile rows. Independent of state size — CA works the same shape
as VT, just more iterations.

Replaces the prior implementation that walked all 11M pl_pfile rows
sequentially and issued one Atlas find_one + one update_one per row
(~60 hours per fire).
"""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException


from pymongo import UpdateOne

from address_dedup import dedupe_addresses
from code_labels import state_label_of, country_label_of
from pipeline_runtime import PipelineRuntime

_log = ChatHealthyLoggingService()


_PL_ADDR1 = "Provider Secondary Practice Location Address- Address Line 1"
_PL_ADDR2 = "Provider Secondary Practice Location Address-  Address Line 2"
_PL_CITY = "Provider Secondary Practice Location Address - City Name"
_PL_STATE = "Provider Secondary Practice Location Address - State Name"
_PL_ZIP = "Provider Secondary Practice Location Address - Postal Code"
_PL_COUNTRY = "Provider Secondary Practice Location Address - Country Code (If outside U.S.)"
_PL_PHONE = "Provider Secondary Practice Location Address - Telephone Number"

_PROVIDER_CHUNK = 1000
_BULK_CHUNK = 1000


def _extract_partition_state(ctx) -> str:
    partition = ctx.config.get("partition") or {}
    state = (partition.get("business_address_state") or "").upper().strip()
    if not state:
        raise ChatHealthyException(
            mode="runtime_error",
            message="attach_practice_addresses: per-state fanout required "
                    "but ctx.partition.business_address_state is empty",
            component="attach_practice_addresses",
        )
    return state


def _pl_row_to_address(row: dict) -> dict | None:
    raw = row.get("raw") or {}
    line1 = (raw.get(_PL_ADDR1) or "").strip()
    city = (raw.get(_PL_CITY) or "").strip()
    pl_state = (raw.get(_PL_STATE) or "").strip()
    if not (line1 and city and pl_state):
        return None
    country = (raw.get(_PL_COUNTRY) or "US").strip() or "US"
    addr = {
        "line1": line1,
        "line2": (raw.get(_PL_ADDR2) or "").strip() or None,
        "city": city,
        "state": pl_state,
        # Truncate to 5-digit ZIP at ingest (mirrors normalize_provider_rows
        # for primary practice + business). pl_pfile carries some 9-digit
        # no-hyphen ZIP+4 forms like "056724776" — take only the first 5.
        "zip": (raw.get(_PL_ZIP) or "").strip().split("-", 1)[0][:5],
        "country": country,
        "phone": (raw.get(_PL_PHONE) or "").strip() or None,
        "address_type": "secondary_practice",
        "primary": False,
        "source": "pl_pfile",
        "county": {"fips": None},
    }
    # LLD v39 sec. 7.1: state and country get sibling _label fields.
    slbl = state_label_of(pl_state)
    if slbl:
        addr["state_label"] = slbl
    clbl = country_label_of(country)
    if clbl:
        addr["country_code_label"] = clbl
    return addr


def _fetch_pl_by_npi(pl_coll, run_id: str, npis: list[str]) -> dict[str, list[dict]]:
    pl_by_npi: dict[str, list[dict]] = {}
    for row in pl_coll.find({"run_id": run_id, "npi": {"$in": npis}}):
        npi = str(row.get("npi") or "")
        if not npi:
            continue
        incoming = _pl_row_to_address(row)
        if incoming is None:
            continue
        pl_by_npi.setdefault(npi, []).append(incoming)
    return pl_by_npi


def _flush(providers_coll, ops_buffer: list[UpdateOne]) -> int:
    if not ops_buffer:
        return 0
    result = providers_coll.bulk_write(ops_buffer, ordered=False)
    return result.modified_count


def attach_practice_addresses(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    state = _extract_partition_state(ctx)
    pl_coll = rt.staging_coll("pl_pfile")

    ops_buffer: list[UpdateOne] = []
    providers_scanned = 0
    providers_updated = 0
    addresses_attached = 0
    pl_rows_matched = 0

    chunk: list[dict] = []

    def _process_chunk(chunk: list[dict]) -> None:
        nonlocal pl_rows_matched, addresses_attached, providers_updated, ops_buffer
        npis = [str(d.get("npi")) for d in chunk if d.get("npi")]
        if not npis:
            return
        pl_by_npi = _fetch_pl_by_npi(pl_coll, rt.run_id, npis)
        pl_rows_matched += sum(len(v) for v in pl_by_npi.values())
        for doc in chunk:
            npi = str(doc.get("npi") or "")
            new_addrs = pl_by_npi.get(npi)
            if not new_addrs:
                continue
            existing = list(doc.get("practice_addresses") or [])
            merged = dedupe_addresses([*existing, *new_addrs])
            ops_buffer.append(
                UpdateOne({"npi": npi}, {"$set": {"practice_addresses": merged}}))
            addresses_attached += len(new_addrs)
            if len(ops_buffer) >= _BULK_CHUNK:
                providers_updated += _flush(rt.providers_coll, ops_buffer)
                ops_buffer = []

    # NPI-atomic ownership: match on BUSINESS mailing address (single-
    # valued per NPPES contract, so $elemMatch gives exactly one owner
    # per NPI). Practice addresses are optional and multi-valued and
    # cannot serve as the atomic partition key.
    for doc in rt.providers_coll.find(
        {"business_address.state": state},
        {"npi": 1, "business_address": 1, "practice_addresses": 1},
    ):
        providers_scanned += 1
        chunk.append(doc)
        if len(chunk) >= _PROVIDER_CHUNK:
            _process_chunk(chunk)
            chunk = []

    if chunk:
        _process_chunk(chunk)
    if ops_buffer:
        providers_updated += _flush(rt.providers_coll, ops_buffer)

    return {
        "state": state,
        "providers_scanned": providers_scanned,
        "pl_pfile_rows_matched": pl_rows_matched,
        "providers_updated": providers_updated,
        "addresses_attached": addresses_attached,
    }
