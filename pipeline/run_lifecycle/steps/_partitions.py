# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""State and county partition helpers.

A run's scope is one of exactly two shapes: an explicit list of state codes,
or ["ALL"]. ALL fans out to one worker per US state (the fifty plus DC) and
one more worker, keyed "ALL", that owns every provider whose business state
is none of those -- the territories, the military codes AA/AE/AP, foreign
addresses and blank-state rows. Fifty-two workers, and together they cover
every record.
"""

from __future__ import annotations

ALL_US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
]


def business_state_filter(state: str | None) -> dict:
    """The Mongo predicate selecting one partition's providers by business
    mailing state.

    No state means no filter -- an unpartitioned step reading the whole
    collection. "ALL" is the catch-all worker: every provider whose business
    state is none of ALL_US_STATES. Any other value is that literal state.
    """
    if not state:
        return {}
    if state == "ALL":
        return {"business_address.state": {"$nin": list(ALL_US_STATES)}}
    return {"business_address.state": state}


def staged_state_filter(state: str | None) -> dict:
    """business_state_filter's twin for a staging row, which carries the state
    on a flat `state` field rather than under business_address. Same three
    cases; only the field name differs, so neither can borrow the other.
    """
    if not state:
        return {}
    if state == "ALL":
        return {"state": {"$nin": list(ALL_US_STATES)}}
    return {"state": state}


def is_full_scope(states: list[str] | None) -> bool:
    """True when a scope is the whole country -- the single token ALL."""
    return bool(states) and len(states) == 1 and str(states[0]).upper() == "ALL"


def state_partitions(states: list[str]) -> list[dict]:
    if is_full_scope(states):
        return ([{"business_address_state": s} for s in ALL_US_STATES]
                + [{"business_address_state": "ALL"}])
    return [{"business_address_state": s} for s in states]


def state_entity_partitions(states: list[str]) -> list[dict]:
    """One partition per (state, entity type). Type 1 and Type 2 are disjoint
    -- an NPI carries exactly one Entity Type Code -- so the two never write
    the same document and neither waits on the other.
    """
    return [dict(part, entity_type=t)
            for part in state_partitions(states)
            for t in (1, 2)]


def county_partitions(states: list[str]) -> list[dict]:
    """One worker per state, never sub-partitioned: two workers at the same
    provider doc $set-clobber each other's address array.
    """
    return state_partitions(states)
