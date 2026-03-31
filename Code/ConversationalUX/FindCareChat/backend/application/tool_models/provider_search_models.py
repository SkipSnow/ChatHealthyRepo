# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pydantic models for provider search tool (ARCH-001 Phase 6)

from pydantic import BaseModel, Field
from typing import Optional


class ProviderSearchInput(BaseModel):
    specialty_query: str = Field(..., description="What kind of provider to find")
    state: str = Field(..., description="Two-letter state code")
    city: str = Field("", description="Optional city filter")
    county: str = Field("", description="Optional county filter")
    limit: int = Field(5, description="Max results")


class SpecialtyInput(BaseModel):
    query: str = Field(..., description="Specialty to search for")
