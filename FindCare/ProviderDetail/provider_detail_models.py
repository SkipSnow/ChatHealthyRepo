# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Pydantic I/O contracts for the ProviderDetail tool.

Each display type owns its `from_stored` classmethod that knows how to
project from the stored MongoDB provider record. The
ProviderDetailService orchestrates the construction but the conversion
logic lives next to the type definition.

Shape mirrors the provider record sections the panel renders per
EPIC-006-F-025-S-001: identity + addresses[] + licenses[] + insurance[]
+ taxonomies[] + research_sites. Empty arrays render as labeled empty
sections per REQ-B-003 / REQ-B-006 — no fallback prose.
"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, HttpUrl


class ProviderDetailInput(BaseModel):
    """Fields the provider card currently shows on the screen."""
    name: str = Field(..., description="Provider display name")
    npi: str = Field(..., description="National Provider Identifier")
    specialty: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None)
    county: Optional[str] = Field(default=None)
    phone: Optional[str] = Field(default=None)
    state: Optional[str] = Field(default=None)


class Identity(BaseModel):
    """Identity + credential block per S-001-REQ-B-002."""
    name_prefix: str = ""
    first_name: str = ""
    middle_name: str = ""
    last_name: str = ""
    credentials: str = ""
    npi: str = ""
    primary_taxonomy_display: str = ""
    status_active: bool = True
    enumeration_date: str = ""

    @classmethod
    def from_stored(cls, stored: dict, primary_taxonomy_display: str) -> "Identity":
        active = stored.get("active") or []
        status_active = bool(active[-1].get("is_active", True)) if active else True
        return cls(
            name_prefix=stored.get("provider_name_prefix_text", "") or "",
            first_name=stored.get("provider_first_name", "") or "",
            middle_name=stored.get("provider_middle_name", "") or "",
            last_name=stored.get("provider_last_name_legal_name", "") or "",
            credentials=stored.get("provider_credential_text", "") or "",
            npi=stored.get("npi", "") or "",
            primary_taxonomy_display=primary_taxonomy_display,
            status_active=status_active,
            enumeration_date=stored.get("provider_enumeration_date", "") or "",
        )


class County(BaseModel):
    """Per-address county block per S-001-REQ-B-005. urban absent when
    unknown (e.g. brand-new addresses written back from NPPES whose RUCC
    pass has not yet re-run)."""
    name: str = ""
    urban: Optional[bool] = None

    @classmethod
    def from_stored(cls, county: dict) -> Optional["County"]:
        if not county:
            return None
        name = county.get("name") or ""
        urban_raw = county.get("urban")
        urban = urban_raw if isinstance(urban_raw, bool) else None
        if not name and urban is None:
            return None
        return cls(name=name, urban=urban)


class Address(BaseModel):
    """Per S-001-REQ-B-004: every address, labeled, practice first, business last."""
    address_type: str = ""
    line1: str = ""
    line2: str = ""
    city: str = ""
    state: str = ""
    zip: str = ""
    country: str = ""
    phone: str = ""
    county: Optional[County] = None

    @classmethod
    def from_stored(cls, address: dict) -> "Address":
        return cls(
            address_type=address.get("address_type", "") or "",
            line1=address.get("line1", "") or "",
            line2=address.get("line2", "") or "",
            city=address.get("city", "") or "",
            state=address.get("state", "") or "",
            zip=address.get("zip", "") or "",
            country=address.get("country", "") or "",
            phone=address.get("phone", "") or "",
            county=County.from_stored(address.get("county") or {}),
        )

    @classmethod
    def ordered_from_stored(cls, stored: dict) -> list["Address"]:
        """Per S-001-REQ-B-004: practice first, business last; others between."""
        # v4 carries the two kinds in their own fields, so the ordering no
        # longer has to be recovered by scanning a mixed array for a type
        # tag, and the 'other' bucket cannot occur -- there is nowhere for a
        # third kind to live.
        practice = list(stored.get("practice_addresses") or [])
        business_one = stored.get("business_address")
        business = [business_one] if business_one else []
        raw = practice + business
        other = [
            a for a in raw
            if a.get("address_type") not in ("practice", "business")
        ]
        return [cls.from_stored(a) for a in (practice + other + business)]


class License(BaseModel):
    """Per S-001-REQ-B-003: every entry in normalized licenses[]."""
    state: str = ""
    number: str = ""

    @classmethod
    def from_stored(cls, license_record: dict) -> "License":
        return cls(
            state=(license_record.get("state") or "").strip(),
            number=(license_record.get("number") or "").strip(),
        )


class Insurance(BaseModel):
    """Per S-001-REQ-B-006: every entry in normalized insurance[]."""
    insurance_type: str = ""
    brand: str = ""
    state: str = ""
    raw_value: str = ""

    @classmethod
    def from_stored(cls, insurance_record: dict) -> "Insurance":
        return cls(
            insurance_type=(insurance_record.get("insurance_type") or "").strip(),
            brand=(insurance_record.get("payer_name") or "").strip(),
            state=(insurance_record.get("state") or "").strip(),
            raw_value=(insurance_record.get("issuer_raw") or "").strip(),
        )


class Taxonomy(BaseModel):
    code: str = ""
    display_name: str = ""
    primary: bool = False

    @classmethod
    def from_stored(cls, taxonomy: dict, code_to_display: dict[str, str]) -> "Taxonomy":
        code = (taxonomy.get("code") or "").strip()
        return cls(
            code=code,
            display_name=code_to_display.get(code, ""),
            primary=bool(taxonomy.get("primary")),
        )


class ResearchSite(BaseModel):
    """One external research link. URL is HttpUrl-validated."""
    url: HttpUrl
    name: str
    guidance: str


class ProviderDetailOutput(BaseModel):
    """Output JSON the front-end widget renders."""
    provider_name: str
    npi: str
    identity: Identity
    addresses: list[Address] = Field(default_factory=list)
    licenses: list[License] = Field(default_factory=list)
    insurance: list[Insurance] = Field(default_factory=list)
    taxonomies: list[Taxonomy] = Field(default_factory=list)
    research_sites: dict[str, ResearchSite] = Field(default_factory=dict)

    @classmethod
    def from_stored(
        cls,
        provider_name: str,
        npi: str,
        stored: Optional[dict],
        code_to_display: dict[str, str],
        primary_taxonomy_display: str,
        research_sites: dict[str, dict],
    ) -> "ProviderDetailOutput":
        if stored is None:
            return cls(
                provider_name=provider_name,
                npi=npi,
                identity=Identity(npi=npi),
                research_sites={k: ResearchSite(**v) for k, v in research_sites.items()},
            )
        return cls(
            provider_name=provider_name,
            npi=npi,
            identity=Identity.from_stored(stored, primary_taxonomy_display),
            addresses=Address.ordered_from_stored(stored),
            licenses=[License.from_stored(lic) for lic in stored.get("licenses") or []],
            insurance=[Insurance.from_stored(ins) for ins in stored.get("insurance") or []],
            taxonomies=[
                Taxonomy.from_stored(t, code_to_display)
                for t in stored.get("taxonomies") or []
            ],
            research_sites={k: ResearchSite(**v) for k, v in research_sites.items()},
        )

    @staticmethod
    def primary_taxonomy_code(stored: dict) -> str:
        tax = stored.get("taxonomies") or []
        primary = next((t for t in tax if t.get("primary")), None)
        if primary:
            return (primary.get("code") or "").strip()
        if tax:
            return (tax[0].get("code") or "").strip()
        return ""

    @staticmethod
    def primary_practice_state(stored: dict) -> str:
        # Practice first, then the business address -- the same order the
        # old scan produced, expressed as the two fields that now hold them.
        for a in stored.get("practice_addresses") or []:
            if a.get("state"):
                return (a.get("state") or "").strip().upper()
        business = stored.get("business_address") or {}
        if business.get("state"):
            return (business.get("state") or "").strip().upper()
        return ""
