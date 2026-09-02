# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pydantic models for provider search tool (ARCH-001 Phase 6)

from pydantic import BaseModel, Field
from typing import Optional


class ProviderSearchInput(BaseModel):
    entity_type: str = Field(..., description="The entity type the page returns. A property of the page, not a filter the caller may omit.")
    specialty_query: str = Field("", description="What kind of provider to find (natural language)")
    state: str = Field("", description="Two-letter state code")
    city: str = Field("", description="Optional city filter")
    county: str = Field("", description="Optional county filter")
    zip: str = Field("", description="Optional ZIP code filter (five digits)")
    limit: int = Field(25, description="Max results per page (5-100)")
    npi: str = Field("", description="Exact NPI lookup — returns one provider")
    name: str = Field("", description="Provider name search")
    nucc_codes: list[str] = Field(default_factory=list, description="NUCC taxonomy codes (10-char strings) to filter by directly")


class SpecialtyInput(BaseModel):
    query: str = Field(..., description="Specialty to search for")
