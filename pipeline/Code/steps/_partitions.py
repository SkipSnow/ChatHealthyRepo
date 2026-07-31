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


def county_partitions(states: list[str]) -> list[dict]:
    # One worker per state -- period. The prior split by (state, kind)
    # sent two workers at the same provider doc for any provider with
    # BOTH a practice and a secondary_practice address, causing full-
    # array $set clobber (proven with NPI 1962405589). Operator rule
    # 2026-07-31: worker scope = state, never sub-partition.
    return state_partitions(states)
