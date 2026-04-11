# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from pydantic import BaseModel, Field


class ClinicalTrialsInput(BaseModel):
    condition: str = Field(..., description="Medical condition to search trials for")
    location: str = Field("", description="Location filter")
    user_location: str = Field("", description="User's location for travel time — any location worldwide")
    max_results: int = Field(5, description="Max results")


class ProviderDetailInput(BaseModel):
    provider_name: str = Field(..., description="Provider name to look up")
    npi: str = Field("", description="NPI number")
    state: str = Field("", description="State code")
