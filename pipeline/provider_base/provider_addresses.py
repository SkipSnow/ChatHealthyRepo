# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Walk both of the provider record's address fields.

The record carries the business mailing address as one object and the practice
locations as an array:

    business_address   : {...}        exactly one, or absent
    practice_addresses : [{...}, ...] zero or more

They were a single addresses[] array discriminated by address_type. The split
is not cosmetic: business_address.state is a scalar path, so it can share a
compound index with taxonomies.code. Two array paths cannot, which is what
forced the query planner to seek on one and post-filter with the other.

Most code wants one kind or the other and reads the field directly. This is
for the few callers that must consider every address a provider has, whichever
kind it is.
"""
from __future__ import annotations


def all_addresses(doc: dict) -> list[dict]:
    """Business first, then practice locations.

    Business first because the old addresses[] put it first, so a caller that
    took entry zero was taking the business address.
    """
    business = doc.get("business_address")
    practice = doc.get("practice_addresses")
    return (([business] if isinstance(business, dict) else [])
            + (list(practice) if isinstance(practice, list) else []))
