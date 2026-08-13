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


def state_partitions(states: list[str]) -> list[dict]:
    if states == ["ALL"] or not states:
        return [{"business_address_state": s} for s in ALL_US_STATES] + [{"business_address_state": "ALL_OTHERS"}]
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
