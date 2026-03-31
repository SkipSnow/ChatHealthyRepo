# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

from pydantic import BaseModel, Field
from typing import Optional


class LeadInput(BaseModel):
    email: str = Field("", description="User's email")
    name: str = Field("Name not provided", description="User's name")
    notes: str = Field("not provided", description="Notes")
    message: str = Field("", description="User's message")
    consent_verbatim: bool = Field(False, description="Verbatim consent granted")
    consent_summary: Optional[bool] = Field(None, description="Summary consent granted")


class UnknownInput(BaseModel):
    question: str = Field(..., description="The unanswerable question")
    question_class: str = Field("irrelevant", description="healthcare_capability | medical_advice | irrelevant")
    consent: bool = Field(False, description="User consented to recording")
