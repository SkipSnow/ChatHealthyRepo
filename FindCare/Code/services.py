# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""FindCare service instances, drained out of app.py.

app.py collects a request and hands it to a tool; it does not construct the
engines. The mining/search engines live here as configured singletons so the
tools that own the work (specialty_filter, provider_search) reach the same
instances app.py's thin routes hand off to, without importing the FastAPI
application module.
"""
from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException

from db_config import ENV_PREFIX, get_db
from embedding_client import EmbeddingClient
from SpecialtyFilter.filter import SpecialtyFilter, SECTION_INDIVIDUAL
from ProviderManagement.provider_search_service import FindCareService

# The canonical embedding model, shared by provider vector search and
# specialty matching (EPIC-008-F-011-S-004).
embedding_client = EmbeddingClient()

# The NUCC specialty engine the specialty_filter tool owns.
specialty_service = SpecialtyFilter(
    get_db_fn=get_db, env_prefix=ENV_PREFIX,
    get_vector_fn=embedding_client.get_specialty_vector)

# The provider search engine the provider_search tool owns.
find_care = FindCareService(
    get_db_fn=get_db, env_prefix=ENV_PREFIX, specialty_service=specialty_service)


# ── specialty resolution + panel shaping (drained from app.py) ──────────────
# The specialty_filter tool owns turning a complaint into the kinds of care
# giver that treat it, and the sets the panel offers as one gesture.
async def resolve_specialties(complaint: str, prior_complaint: str = "",
                              cached: list = None) -> dict:
    """The kinds of care giver that treat a complaint, in the panel's shape.

    SpecialtyFilter owns the cache-vs-LLM decision: given the prior complaint
    and the specialties already resolved for it, its model decides whether
    this request materially changed. When it did not, the cached panel comes
    straight back; when it did, it is re-resolved and `why` explains it."""
    resolved = await specialty_service.find_specialties(
        complaint, None, SECTION_INDIVIDUAL, prior_complaint, cached)
    if "error" in resolved:
        raise ChatHealthyException(
            mode="complaint_unresolved",
            component="FindCareBackend",
            message=f"complaint {complaint!r} did not resolve: {resolved['error']}")
    if resolved.get("reused"):
        # The cache is already in panel shape (that is what we handed in).
        return {"specialties": list(cached or []),
                "complaint": str(resolved.get("complaint") or "").strip()
                             or prior_complaint or complaint,
                "reused": True, "why": ""}
    return {
        "specialties": [{"code": row["Code"], "name": row["Display Name"],
                         "can_prescribe": row.get("can_prescribe", False),
                         "homeopathic": row.get("homeopathic", False),
                         "rank": row.get("rank", 0)}
                        for row in resolved.get("specialties", [])],
        "complaint": str(resolved.get("complaint") or "").strip() or complaint,
        "reused": False, "why": str(resolved.get("why") or ""),
    }


def ticked(offered: list[dict]) -> list[str]:
    """Which offered rows the panel paints ticked -- the prescribers."""
    return [row["code"] for row in offered if row.get("can_prescribe")]


def specialty_groups(offered: list[dict]) -> dict:
    """The sets the panel offers as one gesture."""
    return {
        "all_codes": [row["code"] for row in offered if row.get("code")],
        "prescriber_codes": [row["code"] for row in offered
                             if row.get("can_prescribe")],
        "homeopathic_codes": [row["code"] for row in offered
                              if row.get("homeopathic")],
        "default_selected_codes": ticked(offered),
    }
