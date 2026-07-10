# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from __future__ import annotations

from county_cascade_engine import finalize_unresolved_counties


def test_finalize_unresolved_practice_addresses():
    doc = {
        "addresses": [
            {"address_type": "primary_practice", "county": {}},
            {"address_type": "business", "county": {"fips": "56001"}},
        ]
    }
    finalize_unresolved_counties(doc)
    county = doc["addresses"][0]["county"]
    assert county["source"] == "county_unresolvable"
    assert county["fips"] is None
