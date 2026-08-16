# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Reach the provider record's addresses.

The record carries the business mailing address as one object and the practice
locations as an array:

    business_address   : {...}        exactly one, or absent
    practice_addresses : [{...}, ...] zero or more

They were a single addresses[] array discriminated by address_type. The split
is not cosmetic: business_address.state is a scalar path, so it can share a
compound index with taxonomies.code. Two array paths cannot, which is what
forced the query planner to seek on one and post-filter with the other.

The address object itself is unchanged and identical in both places, so code
that does not care which kind it holds can walk all_addresses(doc).
"""
from __future__ import annotations

BUSINESS_STATE = "business_address.state"


def business_address(doc: dict) -> dict | None:
    """The provider's business mailing address, or None."""
    value = doc.get("business_address")
    return value if isinstance(value, dict) else None


def practice_addresses(doc: dict) -> list[dict]:
    """Every practice location. Never None."""
    value = doc.get("practice_addresses")
    return list(value) if isinstance(value, list) else []


def all_addresses(doc: dict) -> list[dict]:
    """Business first, then practice locations.

    Business first because the old addresses[] put it first and callers that
    took entry zero were taking the business address.
    """
    business = business_address(doc)
    return ([business] if business else []) + practice_addresses(doc)


def business_state(doc: dict) -> str | None:
    """The state that owns this provider's partition."""
    business = business_address(doc)
    return business.get("state") if business else None


def in_state(state: str) -> dict:
    """The query selecting providers whose business address is in a state.

    One scalar equality where it used to be an $elemMatch over an array, which
    is what makes the compound index usable.
    """
    return {BUSINESS_STATE: state}


def in_states(states) -> dict:
    """The same for a set of states."""
    return {BUSINESS_STATE: {"$in": list(states)}}
