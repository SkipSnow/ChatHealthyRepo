# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Within-record address helpers — LLD §4.9.3."""

from __future__ import annotations


def address_location_key(addr: dict) -> str:
    """Hash key for dedup: line1|city|state|zip (5-digit)."""
    line1 = (addr.get("line1") or "").strip().upper()
    city = (addr.get("city") or "").strip().upper()
    state = (addr.get("state") or "").strip().upper()
    zip5 = (addr.get("zip") or "")[:5]
    return f"{line1}|{city}|{state}|{zip5}"


_ADDRESS_TYPE_RANK = {
    "practice": 4,
    "secondary_practice": 3,
    "business": 2,
    "mailing": 1,
}


def merge_address(existing: dict, incoming: dict) -> dict:
    """Prefer incoming fields but keep the most-specific address_type and
    keep county/urban/phone/fax from whichever address had them populated.

    Fixes a data-loss bug: prior implementation was {**existing, **incoming}
    so incoming.address_type unconditionally overwrote existing.address_type.
    When NPPES practice and mailing addresses share a building (small
    clinic), dedupe collapsed them and address_type=practice was silently
    overwritten by address_type=business, then again by
    address_type=secondary_practice when attach_practice_addresses ran a
    pl_pfile row at the same location. Every such provider ended up with
    no `practice` address at all.

    Rule: address_type is kept from whichever input carries the more
    specific type (practice > secondary_practice > business > mailing).
    Every OTHER field: incoming wins if present, else existing.
    """
    merged = {**existing, **incoming}
    existing_type = (existing.get("address_type") or "").strip().lower()
    incoming_type = (incoming.get("address_type") or "").strip().lower()
    if existing_type or incoming_type:
        if _ADDRESS_TYPE_RANK.get(existing_type, 0) >= _ADDRESS_TYPE_RANK.get(incoming_type, 0):
            merged["address_type"] = existing_type or incoming_type
        else:
            merged["address_type"] = incoming_type or existing_type
    for field in ("county", "urban", "phone", "fax"):
        if existing.get(field) is not None and incoming.get(field) is None:
            merged[field] = existing[field]
    return merged


def dedupe_addresses(addresses: list[dict]) -> list[dict]:
    """Collapse addresses[] to one entry per location key (LLD §4.9.3)."""
    by_key: dict[str, dict] = {}
    for addr in addresses:
        if not isinstance(addr, dict):
            continue
        key = address_location_key(addr)
        if key in ("|||", "||"):
            continue
        if key not in by_key:
            by_key[key] = addr
        else:
            by_key[key] = merge_address(by_key[key], addr)
    return list(by_key.values())
