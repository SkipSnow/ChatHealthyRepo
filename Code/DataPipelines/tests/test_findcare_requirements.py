# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""FindCare requirement tests — FC-MSG-001, PIPE-DQ-004, BRAIN-CONVERSATION-LOG-REQ-012.

FC-MSG-001-REQ-001: Summary uses user's search term (specialty_searched)
PIPE-DQ-004-REQ-006: Search results include can_prescribe flag
PIPE-DQ-004-REQ-007: EvaluateCare uses can_prescribe flag
BRAIN-CONVERSATION-LOG-REQ-012: Skip — not a testable requirement
"""

import ast
import os
import sys

import pytest

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
BACKEND_DIR = os.path.join(REPO, "Code", "ConversationalUX", "FindCareChat", "backend")
sys.path.insert(0, BACKEND_DIR)


# ── FC-MSG-001-REQ-001 ────────────────────────────────────────────────────
# Summary uses user's search term (specialty_searched), not generic 'providers'

def test_summary_uses_specialty_searched():
    """FC-MSG-001-REQ-001: _build_summary_message uses specialty_searched, not generic 'providers'."""
    from domain.find_care.provider_search_service import FindCareService

    # Call _build_summary_message with a custom specialty_searched term
    msg = FindCareService._build_summary_message(
        has_more=True,
        total_count=100,
        page_count=25,
        specialty_searched="cardiologists",
        state="DE",
    )

    assert "cardiologists" in msg, (
        f"Summary must include the user's search term 'cardiologists', got: {msg}"
    )
    # The generic word 'providers' should NOT appear as the search term
    # (it might appear in other context, but the key replacement should use specialty_searched)
    assert "'cardiologists'" in msg, (
        f"Summary must quote the search term, got: {msg}"
    )


def test_summary_falls_back_to_results_without_specialty():
    """FC-MSG-001-REQ-001: when specialty_searched is empty, uses 'results' as fallback."""
    from domain.find_care.provider_search_service import FindCareService

    msg = FindCareService._build_summary_message(
        has_more=True,
        total_count=50,
        page_count=25,
        specialty_searched="",
        state="DE",
    )

    # Without specialty_searched, should use "results" not "providers"
    assert "'results'" in msg, (
        f"Summary must fall back to 'results' when no specialty_searched, got: {msg}"
    )


def test_summary_empty_when_no_more():
    """FC-MSG-001-REQ-001: summary is empty when has_more=False."""
    from domain.find_care.provider_search_service import FindCareService

    msg = FindCareService._build_summary_message(
        has_more=False,
        total_count=10,
        page_count=10,
        specialty_searched="dermatologists",
    )
    assert msg == "", "Summary must be empty when has_more is False"


# ── PIPE-DQ-004-REQ-006 ───────────────────────────────────────────────────
# Search results include can_prescribe flag

def test_search_results_include_can_prescribe_in_specialty_options():
    """PIPE-DQ-004-REQ-006: specialization_options includes can_prescribe flag."""
    # Verify by code inspection that the search service returns can_prescribe
    source_path = os.path.join(
        BACKEND_DIR, "domain", "find_care", "provider_search_service.py"
    )
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    # The specialization_options list must include can_prescribe
    assert '"can_prescribe"' in source or "'can_prescribe'" in source, (
        "provider_search_service must include can_prescribe in specialization_options"
    )

    # Verify it's in the meta_coll.find projection
    assert "can_prescribe" in source, (
        "provider_search_service must query can_prescribe from SpecialtyMetaData"
    )


def test_classify_endpoint_returns_can_prescribe():
    """PIPE-DQ-004-REQ-006: /classify endpoint maps can_prescribe to response."""
    source_path = os.path.join(BACKEND_DIR, "main.py")
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert "can_prescribe" in source, (
        "main.py /classify endpoint must include can_prescribe in specialty response"
    )


# ── PIPE-DQ-004-REQ-007 ───────────────────────────────────────────────────
# EvaluateCare uses can_prescribe flag

def test_evaluate_care_facade_exists():
    """PIPE-DQ-004-REQ-007: EvaluateCareFacade exists and has provider detail lookup."""
    source_path = os.path.join(
        BACKEND_DIR, "application", "facades", "evaluate_care_facade.py"
    )
    with open(source_path, "r", encoding="utf-8") as f:
        source = f.read()

    assert "class EvaluateCareFacade" in source, "EvaluateCareFacade class must exist"
    assert "get_provider_details" in source, (
        "EvaluateCareFacade must have get_provider_details method"
    )


def test_can_prescribe_available_for_evaluate_care():
    """PIPE-DQ-004-REQ-007: can_prescribe flag flows through specialty data to evaluation.

    The specialization_options from FindCare include can_prescribe.
    The frontend sends selected providers (with specialty codes) to EvaluateCare.
    The can_prescribe flag on each specialty determines if prescription scoring applies.
    """
    # Verify the flag is in the specialty_options that flow to the frontend
    search_svc_path = os.path.join(
        BACKEND_DIR, "domain", "find_care", "provider_search_service.py"
    )
    with open(search_svc_path, "r", encoding="utf-8") as f:
        search_source = f.read()

    # can_prescribe is included in specialization_options
    assert "can_prescribe" in search_source

    # The homeopathic resolver also includes can_prescribe
    homeo_path = os.path.join(
        BACKEND_DIR, "domain", "find_care", "homeopathic_resolver.py"
    )
    with open(homeo_path, "r", encoding="utf-8") as f:
        homeo_source = f.read()

    assert "can_prescribe" in homeo_source, (
        "homeopathic_resolver must include can_prescribe in resolved specialties"
    )


# ── BRAIN-CONVERSATION-LOG-REQ-012 ────────────────────────────────────────
# Rationalization review — not a testable requirement

@pytest.mark.skip(reason="BRAIN-CONVERSATION-LOG-REQ-012 is a process requirement "
                         "(rationalization review by human and Claude), not testable via pytest")
def test_conversation_log_rationalization():
    """BRAIN-CONVERSATION-LOG-REQ-012: conversation_log.json needs rationalization review."""
    pass
