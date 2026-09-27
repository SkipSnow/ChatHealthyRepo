# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Within-record dedup for licenses[] and insurance[] — LLD §4.8 / §4.9."""

from __future__ import annotations


def _license_id(entry: dict) -> str:
    return (entry.get("license_id") or entry.get("number") or "").strip()


def _normalize_license_id_for_key(license_id: str) -> str:
    """Strip non-alphanumeric characters so 'C10002916' and 'C1-0002916'
    dedupe as the same license. NPPES license numbers show up in both
    the raw main-file License_Number column and the Other Provider
    Identifier slots with slightly different formatting (hyphens, spaces)
    that would otherwise defeat dedup."""
    return "".join(ch for ch in license_id.upper() if ch.isalnum())


def license_key(entry: dict) -> tuple[str, str]:
    """Unique per license within one NPI: (state, normalized_license_id)."""
    return (
        (entry.get("state") or "").strip().upper(),
        _normalize_license_id_for_key(_license_id(entry)),
    )


def insurance_key(entry: dict) -> tuple[str, str]:
    """Unique per insurance slot within one NPI: (payer_name, state).

    Was (insurance_type, state), which collapsed every commercial payer
    within a state to one entry (COMMERCIAL, VT). Real granularity is
    at the payer_name level (BLUE_CROSS_BLUE_SHIELD, AETNA, CIGNA all
    coexist for the same provider in the same state)."""
    return (
        (entry.get("payer_name") or entry.get("insurance_type") or "").strip().upper(),
        (entry.get("state") or "").strip().upper(),
    )


def _richer(existing: dict, incoming: dict) -> dict:
    """Keep the entry with more populated fields; incoming wins ties on key fields."""
    def score(d: dict) -> int:
        return sum(1 for v in d.values() if v not in (None, "", []))

    return incoming if score(incoming) >= score(existing) else existing


def dedupe_licenses(licenses: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict] = {}
    for entry in licenses:
        if not isinstance(entry, dict):
            continue
        shaped = normalize_license_shape(entry)
        key = license_key(shaped)
        if not key[0] and not key[1]:
            continue
        if key not in by_key:
            by_key[key] = shaped
        else:
            by_key[key] = normalize_license_shape(_richer(by_key[key], shaped))
    return list(by_key.values())


def dedupe_insurance(insurance: list[dict]) -> list[dict]:
    by_key: dict[tuple[str, str], dict] = {}
    for entry in insurance:
        if not isinstance(entry, dict):
            continue
        key = insurance_key(entry)
        if not key[0] and not key[1]:
            continue
        if key not in by_key:
            by_key[key] = dict(entry)
        else:
            by_key[key] = _richer(by_key[key], entry)
    return list(by_key.values())


def merge_licenses(existing: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    return dedupe_licenses([*(existing or []), *(incoming or [])])


def merge_insurance(existing: list[dict] | None, incoming: list[dict] | None) -> list[dict]:
    return dedupe_insurance([*(existing or []), *(incoming or [])])


def normalize_license_shape(entry: dict) -> dict:
    """Map NPPES normalize output (number) to schema license_id when needed."""
    out = dict(entry)
    if not out.get("license_id") and out.get("number"):
        out["license_id"] = str(out["number"]).strip()
    return out
