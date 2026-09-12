# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Shared state-scope predicate for the provider pipeline.

EPIC-010-F-006-S-001 — single source of truth for how every step interprets
the `states` parameter. Used by drain, load worker, enrichment, and embedding
so the predicate is 100% uniform across the pipeline (REQ-T-001).

Behavior:
  - states missing / empty / malformed -> raises ValueError       (REQ-T-002)
  - states == ["ALL"]                  -> full-load sentinel       (REQ-B-002, T-004)
  - any other non-empty list           -> business_address.state predicate
                                          (REQ-T-003). The wider
                                          any-address / licenses /
                                          other_identifiers predicate is gone:
                                          the business address is the
                                          canonical residency signal.

Supports legacy dict input {"mode": "include"|"exclude", "list": [...]} for
backward compatibility with the existing enrichment caller. Exclude mode is
preserved as-is — it predates state scoping and does not interact with the
multi-field predicate.
"""
from __future__ import annotations
from chathealthy_lib.exceptions import ChatHealthyException

ALL_STATES_SENTINEL = "ALL"



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
    an equality on `business_address.state`, which is the canonical
    residency signal for state-scoped runs and the only one the drain step
    keys off. It was an `$elemMatch` over addresses[] while the business
    address was an entry in that array.

    For the legacy dict form, `mode='exclude'` becomes a `$nor` on the same
    predicate — preserving the exclude semantic.
    """
    if is_full_load(states):
        return {}
    if isinstance(states, dict):
        lst = states["list"]
        biz_match = {"business_address.state": {"$in": lst}}
        return biz_match if states["mode"] == "include" else {"$nor": [biz_match]}
    return {"business_address.state": {"$in": list(states)}}


def _business_address_state(doc: dict) -> str:
    """Return the upper-cased state from the provider's business address, or
    empty string if absent.

    State-scoping keys off the business address only — practice addresses,
    licenses and other_identifiers are not part of the state predicate.
    """
    entry = doc.get("business_address")
    if isinstance(entry, dict):
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
