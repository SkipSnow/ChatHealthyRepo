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


def merge_address(existing: dict, incoming: dict) -> dict:
    """Prefer incoming fields but keep county/urban from existing when set."""
    merged = {**existing, **incoming}
    for field in ("county", "urban"):
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
