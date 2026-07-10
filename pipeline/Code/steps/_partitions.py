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
    parts: list[dict] = []
    for st in state_partitions(states):
        state = st["business_address_state"]
        for kind in ("primary_practice", "secondary_practice"):
            parts.append({"business_address_state": state, "kind": kind})
    return parts
