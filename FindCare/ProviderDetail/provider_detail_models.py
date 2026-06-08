# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Pydantic I/O contracts for the ProviderDetail tool.

Input fields mirror the provider-card display fields exactly (one
Pydantic field per on-screen field), per EPIC-006-F-025 'Provider
Detail'. Output is whatever ProviderDetailService.lookup produces —
NPI Registry credentials plus a dict of external research sites.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field


class ProviderDetailInput(BaseModel):
    """Fields the provider card currently shows on the screen."""
    name: str = Field(..., description="Provider display name")
    npi: str = Field(..., description="National Provider Identifier")
    specialty: Optional[str] = Field(default=None,
                                     description="Primary specialty as rendered on the card")
    address: Optional[str] = Field(default=None,
                                   description="One-line practice address")
    county: Optional[str] = Field(default=None,
                                  description="County name")
    phone: Optional[str] = Field(default=None,
                                 description="Practice phone")
    state: Optional[str] = Field(default=None,
                                 description="Two-letter state code — used to select state-specific research sites")


class NpiDetails(BaseModel):
    """Credential details from the NPI Registry. Present when the
    upstream lookup succeeded; absent when it failed."""
    name: str
    npi: str
    status: str
    credential: str
    specialty: str
    license: str
    license_state: str
    address: str
    phone: str
    enumeration_date: str


class ResearchSite(BaseModel):
    """One external research link."""
    url: str
    name: str
    guidance: str


class ProviderDetailOutput(BaseModel):
    """Output JSON the front-end widget renders."""
    provider_name: str
    npi: str
    npi_details: Optional[NpiDetails] = None
    research_sites: dict[str, ResearchSite] = Field(default_factory=dict)
