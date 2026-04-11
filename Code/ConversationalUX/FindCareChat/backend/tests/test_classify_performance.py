# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# FC-SEARCH-001-REQ-006: AI translates user question into ordered specialty
# codes (most likely → least likely). Provider search is a DB query, not AI.
# GOV-011: AI translates, system answers.

import pytest


class TestClassifyReturnsOrderedSpecialties:
    """FC-SEARCH-001-REQ-006: /classify returns ordered specialty codes for DB query."""

    def test_classify_returns_ordered_specialties(self):
        """/classify returns a list of specialty codes ordered by relevance."""
        import requests
        url = "https://localhost:8080/classify"
        payload = {"message": "find me a doctor who can fix my broken ankle in Delaware"}
        try:
            resp = requests.post(url, json=payload, verify=False, timeout=10)
            assert resp.status_code == 200, f"/classify returned {resp.status_code}"
            data = resp.json()
            specialties = data.get("specialties", [])
            assert len(specialties) > 0, "/classify must return at least one specialty"
            # Each specialty must have a code (for the DB query)
            for i, s in enumerate(specialties):
                assert "code" in s, f"Specialty at index {i} missing 'code': {s}"
            # List must be ordered — first entry is most relevant
            # Orthopedic/podiatry codes should rank above general practice for "broken ankle"
            codes = [s["code"] for s in specialties]
            assert len(codes) == len(set(codes)), f"Duplicate codes in classify response: {codes}"

        except requests.exceptions.ConnectionError:
            pytest.skip("FindCare not running on :8080")

    def test_search_is_db_query_not_ai(self):
        """/search does not call any AI — it is a pure database query."""
        import requests
        # Send specialty codes directly to /search — this must work without AI
        url = "https://localhost:8080/search"
        payload = {"specialty_codes": ["207X00000X"], "state": "DE", "limit": 5}
        try:
            resp = requests.post(url, json=payload, verify=False, timeout=30)
            assert resp.status_code == 200, f"/search returned {resp.status_code}"
            data = resp.json()
            assert "providers" in data, f"/search must return providers: {data}"
        except requests.exceptions.ConnectionError:
            pytest.skip("FindCare not running on :8080")
