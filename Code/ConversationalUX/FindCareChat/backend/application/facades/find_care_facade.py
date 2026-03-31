# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# FindCareFacade — entry point for all FindCare business capabilities.
#
# Facade-to-facade only: EvaluateCareFacade calls FindCareFacade methods,
# never internal services directly. Per Boss directive.
#
# Design: ARCH-001

import logging

from domain.find_care.provider_search_service import ProviderSearchService

_log = logging.getLogger("findcare.facade.find_care")


class FindCareFacade:
    """Public interface for the FindCare business component.

    UAT Features: 1 (Provider Search), 2 (Specialty Identification)
    """

    def __init__(self, provider_search: ProviderSearchService, find_specialty_fn=None):
        self._provider_search = provider_search
        self._find_specialty_fn = find_specialty_fn

    def search_providers(self, specialty_query: str, state: str, city: str = "",
                         county: str = "", limit: int = 5) -> dict:
        """UAT Feature 1: Search for healthcare providers."""
        return self._provider_search.search(
            specialty_query=specialty_query,
            state=state,
            city=city,
            county=county,
            limit=limit,
            find_specialty_fn=self._find_specialty_fn,
        )

    def get_provider_location(self, npi: str) -> dict:
        """Return provider location for cross-domain travel calculations.

        Called by EvaluateCareFacade for clinical trial travel info.
        """
        # Phase 2: delegates to provider search's DB for now
        # Future: dedicated provider lookup
        return {"npi": npi, "status": "not_yet_implemented"}
