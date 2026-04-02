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
from domain.find_care.specialty_service import SpecialtyService

_log = logging.getLogger("findcare.facade.find_care")


class FindCareFacade:
    """Public interface for the FindCare business component.

    UAT Features: 1 (Provider Search), 2 (Specialty Identification)
    """

    def __init__(self, provider_search: ProviderSearchService, specialty: SpecialtyService = None):
        self._provider_search = provider_search
        self._specialty = specialty

    def search_providers(self, specialty_query: str = "", state: str = "",
                         city: str = "", county: str = "", limit: int = 5,
                         npi: str = "", name: str = "", fuzzy_specialty: str = "",
                         specialty_codes: list[str] = None) -> dict:
        """UAT Feature 1: Search for healthcare providers.

        Args:
            specialty_query: natural language specialty search
            state: state abbreviation (DE, MS, VA)
            city: city name filter
            county: county name filter
            limit: max results (default 5)
            npi: exact NPI lookup
            name: provider name search
            fuzzy_specialty: loose specialty text match
            specialty_codes: list of NUCC taxonomy codes to filter by
        """
        return self._provider_search.search(
            specialty_query=specialty_query,
            state=state,
            city=city,
            county=county,
            limit=limit,
            npi=npi,
            name=name,
            fuzzy_specialty=fuzzy_specialty,
            specialty_codes=specialty_codes or [],
            find_specialty_fn=self.identify_specialty if self._specialty else None,
        )

    def identify_specialty(self, query: str) -> dict:
        """UAT Feature 2: Identify NUCC specialty codes."""
        if not self._specialty:
            return {"error": "SpecialtyService not configured"}
        return self._specialty.find_specialty_codes(query)

    def get_provider_location(self, npi: str) -> dict:
        """Return provider location for cross-domain travel calculations.

        Called by EvaluateCareFacade for clinical trial travel info.
        """
        # Phase 2: delegates to provider search's DB for now
        # Future: dedicated provider lookup
        return {"npi": npi, "status": "not_yet_implemented"}
