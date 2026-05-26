# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Shared state-scope predicate for the provider pipeline.

EPIC-010-F-006-S-001 — single source of truth for how every step interprets
the `states` parameter. Used by drain, load worker, enrichment, and embedding
so the predicate is 100% uniform across the pipeline (REQ-T-001).

Behavior:
  - states missing / empty / malformed -> raises ValueError       (REQ-T-002)
  - states == ["ALL"]                  -> full-load sentinel       (REQ-B-002, T-004)
  - any other non-empty list           -> multi-state-bearing-field predicate
                                          across addresses[].state (covers both
                                          business + practice entries),
                                          licenses[].state,
                                          other_identifiers[].state (REQ-T-003)

Supports legacy dict input {"mode": "include"|"exclude", "list": [...]} for
backward compatibility with the existing enrichment caller. Exclude mode is
preserved as-is — it predates state scoping and does not interact with the
multi-field predicate.
"""
from __future__ import annotations

ALL_STATES_SENTINEL = "ALL"

# Every state-bearing field on a provider doc. If a future field is added,
# put it here so every pipeline step picks it up automatically.
#
# Post-schema-reconciliation: practice_address + mailing_address unified into
# addresses[] (with address_type discriminator). One Mongo path
# "addresses.state" covers both. other_identifiers[] is now an array of
# objects (each with .state); the old parallel-array `other_identifier_states`
# is gone.
STATE_BEARING_MONGO_FIELDS = (
    "addresses.state",
    "licenses.state",
    "other_identifiers.state",
)


def normalize_states(config: dict) -> list[str]:
    """Validate + normalize the `states` config parameter.

    Returns a list of upper-cased state codes (or `["ALL"]` for the full-load
    sentinel). Raises ValueError on missing/empty/malformed input.
    """
    states = config.get("states")
    if states is None:
        raise ValueError("states parameter is REQUIRED. Cannot process all records.")
    if isinstance(states, dict):
        # legacy {"mode": "include"|"exclude", "list": [...]}
        mode = states.get("mode", "include").lower()
        lst = states.get("list", [])
        if not lst:
            raise ValueError("states.list is empty. Cannot process all records.")
        if mode not in ("include", "exclude"):
            raise ValueError(f"invalid states mode: {mode!r}")
        return {"mode": mode, "list": [s.upper() for s in lst if s]}
    if not isinstance(states, list):
        raise ValueError(f"invalid states format: {states!r}")
    if not states:
        raise ValueError("states list is empty. Cannot process all records.")
    upper = [s.upper() for s in states if s]
    if not upper:
        raise ValueError("states list contains no valid state codes.")
    return upper


def is_full_load(states) -> bool:
    """True iff `states` is the ALL sentinel."""
    return isinstance(states, list) and states == [ALL_STATES_SENTINEL]


def mongo_state_filter(states) -> dict:
    """Build a Mongo filter for the multi-state-bearing-field predicate.

    Returns `{}` (match all) when `states` is the ALL sentinel. Otherwise
    returns an `$or` across every state-bearing field.

    For the legacy dict form, returns the single-field `addresses.state`
    `$in` / `$nin` filter that enrichment has used historically — this
    preserves the existing exclude-mode behavior unchanged after the
    practice_address -> addresses migration.
    """
    if is_full_load(states):
        return {}
    if isinstance(states, dict):
        lst = states["list"]
        op = "$in" if states["mode"] == "include" else "$nin"
        return {"addresses.state": {op: lst}}
    return {"$or": [{f: {"$in": states}} for f in STATE_BEARING_MONGO_FIELDS]}


def _addresses_states(doc: dict) -> list[str]:
    """Return the list of upper-cased states across all entries in
    `addresses` (both business and practice types).

    Post-schema-reconciliation: `addresses` is always a list of address dicts
    (one business + one or more practice). Pre-migration shapes (single dict,
    or separate practice_address/mailing_address) are not supported here —
    every read path is expected to operate against the new shape.
    """
    out: list[str] = []
    for entry in (doc.get("addresses") or []):
        if isinstance(entry, dict):
            st = (entry.get("state") or "").upper()
            if st:
                out.append(st)
    return out


def doc_matches_state(doc: dict, states) -> bool:
    """Row-level matcher equivalent to `mongo_state_filter(states)`.

    Used by the LOAD worker, which evaluates each parsed CSV row in Python
    (not against Mongo). Mirrors the Mongo predicate exactly so load and
    every Mongo-side step stay symmetrical (REQ-T-001).
    """
    if is_full_load(states):
        return True
    if isinstance(states, dict):
        lst = states["list"]
        for st in _addresses_states(doc):
            if st in lst:
                return states["mode"] == "include"
        return states["mode"] != "include"

    for st in _addresses_states(doc):
        if st in states:
            return True
    for lic in (doc.get("licenses") or []):
        st = (lic.get("state") or "").upper()
        if st in states:
            return True
    for oid in (doc.get("other_identifiers") or []):
        if isinstance(oid, dict):
            st = (oid.get("state") or "").upper()
            if st in states:
                return True
    return False
