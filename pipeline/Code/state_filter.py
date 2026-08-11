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
from chathealthy_lib.exceptions import ChatHealthyException

ALL_STATES_SENTINEL = "ALL"

# Every state-bearing field on a provider doc. If a future field is added,
# put it here so every pipeline step picks it up automatically.
#
# Post-schema-reconciliation: practice_address + mailing_address unified into
# addresses[] (with address_type discriminator). One Mongo path
# "addresses.state" covers both. other_identifiers[] is now an array of
# objects (each with .state); the old parallel-array `other_identifier_states`
# is gone.
BUSINESS_ADDRESS_TYPE = "business"


def normalize_states(config: dict) -> list[str]:
    """Validate + normalize the `states` config parameter.

    Returns a list of upper-cased state codes (or `["ALL"]` for the full-load
    sentinel). Raises ValueError on missing/empty/malformed input.
    """
    states = config.get("states")
    if states is None:
        raise ChatHealthyException(mode="value_error", message="states parameter is REQUIRED. Cannot process all records.")
    if isinstance(states, dict):
        # legacy {"mode": "include"|"exclude", "list": [...]}
        mode = states.get("mode", "include").lower()
        lst = states.get("list", [])
        if not lst:
            raise ChatHealthyException(mode="value_error", message="states.list is empty. Cannot process all records.")
        if mode not in ("include", "exclude"):
            raise ChatHealthyException(mode="value_error", message=f"invalid states mode: {mode!r}")
        return {"mode": mode, "list": [s.upper() for s in lst if s]}
    if not isinstance(states, list):
        raise ChatHealthyException(mode="value_error", message=f"invalid states format: {states!r}")
    if not states:
        raise ChatHealthyException(mode="value_error", message="states list is empty. Cannot process all records.")
    upper = [s.upper() for s in states if s]
    if not upper:
        raise ChatHealthyException(mode="value_error", message="states list contains no valid state codes.")
    return upper


def is_full_load(states) -> bool:
    """True iff `states` is the ALL sentinel."""
    return isinstance(states, list) and states == [ALL_STATES_SENTINEL]


def mongo_state_filter(states) -> dict:
    """Build a Mongo filter scoped to the BUSINESS address only.

    Returns `{}` (match all) when `states` is the ALL sentinel. Otherwise
    returns an `$elemMatch` predicate that fires when the provider's
    business-typed address (addresses[].address_type == 'business') has
    a `state` in the requested set. The wider any-address / licenses /
    other_identifiers predicate is gone — business-address is the
    canonical residency signal for state-scoped runs and the only one
    the drain step should key off of (Skip 2026-05-27).

    For the legacy dict form, `mode='exclude'` becomes a `$nor` on the
    same business-address elemMatch — preserving the exclude semantic.
    """
    if is_full_load(states):
        return {}
    if isinstance(states, dict):
        lst = states["list"]
        biz_match = {"addresses": {"$elemMatch": {
            "address_type": BUSINESS_ADDRESS_TYPE,
            "state": {"$in": lst},
        }}}
        return biz_match if states["mode"] == "include" else {"$nor": [biz_match]}
    return {"addresses": {"$elemMatch": {
        "address_type": BUSINESS_ADDRESS_TYPE,
        "state": {"$in": states},
    }}}


def _business_address_state(doc: dict) -> str:
    """Return the upper-cased state from the provider's business-typed address,
    or empty string if absent.

    Post-schema-reconciliation: `addresses` is a list with one
    `address_type='business'` entry (plus zero-or-more `address_type='practice'`
    entries). State-scoping keys off the business entry only — practice
    addresses, licenses, and other_identifiers are no longer part of the
    state predicate.
    """
    for entry in (doc.get("addresses") or []):
        if isinstance(entry, dict) and entry.get("address_type") == BUSINESS_ADDRESS_TYPE:
            return (entry.get("state") or "").upper()
    return ""


def doc_matches_state(doc: dict, states) -> bool:
    """Row-level matcher equivalent to `mongo_state_filter(states)`.

    Used by the LOAD worker, which evaluates each parsed CSV row in Python
    (not against Mongo). Mirrors the Mongo predicate exactly: business
    address state only.
    """
    if is_full_load(states):
        return True
    biz_state = _business_address_state(doc)
    if isinstance(states, dict):
        lst = states["list"]
        hit = biz_state in lst
        return hit if states["mode"] == "include" else not hit
    return biz_state in states
