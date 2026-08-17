# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""State and county partition helpers."""

from __future__ import annotations

ALL_US_STATES = [
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
]


ALL_OTHERS = "ALL_OTHERS"


def business_state_filter(state: str | None) -> dict:
    """The Mongo predicate selecting one partition's providers.

    ALL_OTHERS is a sentinel, not a state: it names every provider whose
    business address is outside the fifty-one, including those carrying no
    business state at all. Written as an equality it matched the literal
    string and selected nothing, silently, in whichever step spelled it that
    way. There is one spelling now.
    """
    if not state:
        return {}
    if state == ALL_OTHERS:
        return {"business_address.state": {"$nin": list(ALL_US_STATES)}}
    return {"business_address.state": state}


def staged_state_filter(state: str | None) -> dict:
    """The predicate selecting one partition's rows in a staging collection
    that carries the provider's ACTUAL state on each row.

    business_state_filter reads business_address.state on a provider record.
    This reads a flat `state` field on a harvested row. Both need the same
    sentinel handling and neither can borrow the other's field name, so the
    sentinel is spelled out once here rather than a third and fourth time at
    the call sites.
    """
    if not state:
        return {}
    if state == ALL_OTHERS:
        return {"state": {"$nin": list(ALL_US_STATES)}}
    return {"state": state}


def is_full_scope(states: list[str] | None) -> bool:
    """True when a states list means the whole country rather than a set."""
    return bool(states) and len(states) == 1 and str(states[0]).upper() == "ALL"


def state_partitions(states: list[str]) -> list[dict]:
    if states == ["ALL"] or not states:
        return [{"business_address_state": s} for s in ALL_US_STATES] + [{"business_address_state": ALL_OTHERS}]
    return [{"business_address_state": s} for s in states]


def state_entity_partitions(states: list[str]) -> list[dict]:
    """One partition per (state, entity type) — LLD v45 §5.2.11, which puts
    the fan-out across state AND entity type.

    Type 1 and Type 2 are disjoint sets: an NPI carries exactly one Entity
    Type Code, so the two never write the same document and neither has to
    wait for the other. Running them as two sequential steps doubled the
    wall-clock of the branch phases for nothing -- costly on CA, TX and NY.
    """
    return [dict(part, entity_type=t)
            for part in state_partitions(states)
            for t in (1, 2)]


def county_partitions(states: list[str]) -> list[dict]:
    # One worker per state -- period. The prior split by (state, kind)
    # sent two workers at the same provider doc for any provider with
    # BOTH a practice and a secondary_practice address, causing full-
    # array $set clobber (proven with NPI 1962405589). Operator rule
    # 2026-07-31: worker scope = state, never sub-partition.
    return state_partitions(states)
