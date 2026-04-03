# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pydantic models for provider search tool (ARCH-001 Phase 6)

from pydantic import BaseModel, Field
from typing import Optional


class ProviderSearchInput(BaseModel):
    specialty_query: str = Field("", description="What kind of provider to find (natural language)")
    state: str = Field("", description="Two-letter state code")
    city: str = Field("", description="Optional city filter")
    county: str = Field("", description="Optional county filter")
    limit: int = Field(25, description="Max results per page (5-100)")
    npi: str = Field("", description="Exact NPI lookup — returns one provider")
    name: str = Field("", description="Provider name search")
    fuzzy_specialty: str = Field("", description="Loose specialty match — maps to codes then searches")
    specialty_codes: list[str] = Field(default_factory=list, description="NUCC taxonomy codes to filter by directly")


class SpecialtyInput(BaseModel):
    query: str = Field(..., description="Specialty to search for")
