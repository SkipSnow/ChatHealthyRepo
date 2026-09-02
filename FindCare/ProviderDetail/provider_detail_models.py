# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Pydantic I/O contracts for the ProviderDetail tool.

Each display type owns its `from_stored` classmethod that knows how to
project from the stored MongoDB provider record. The
ProviderDetailService orchestrates the construction but the conversion
logic lives next to the type definition.

Shape mirrors the provider record sections the panel renders per
EPIC-006-F-002-S-001: identity + addresses[] + licenses[] + insurance[]
+ taxonomies[] + research_sites. Empty arrays render as labeled empty
sections per REQ-B-003 / REQ-B-006 — no fallback prose.
"""
from __future__ import annotations

from typing import ClassVar, Optional

from pydantic import BaseModel, Field, HttpUrl


class ProviderDetailInput(BaseModel):
    """Fields the provider card currently shows on the screen.

    The NPI is the only one that identifies anybody; the rest are the
    card's copy of what the record already holds. name is optional because
    a detail is also opened from a recorded selection -- restoring a
    context carries the NPI and no card -- and because the record, not a
    card that may have been painted some time ago, is what the name should
    come from.

    The county the card used to send up is not here. It is never read, and
    a county travelling upward from the client is a second source for a
    value the record already holds on the address it belongs to.
    """
    name: Optional[str] = Field(default=None, description="Provider display name")
    npi: str = Field(..., description="National Provider Identifier")
    # The gateway's signature, verified before anything else happens.
    session_token: Optional[dict] = Field(default=None)
    # Which page opened this detail. A property of the page, so the panel
    # shows the identity that page's records carry and the registry check
    # of EPIC-006-F-007-S-003-REQ-B-003 applies on the facility page.
    entity_type: str = Field(default="1")
    specialty: Optional[str] = Field(default=None)
    address: Optional[str] = Field(default=None)
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


# The registry writes this where it holds no value. It is the registry
# saying the field is absent, not a value the record carries, so a panel
# that printed it would be showing the person a placeholder as a fact.
_REGISTRY_ABSENT = "<UNAVAIL>"


def _present(value) -> str:
    text = (value or "").strip() if isinstance(value, str) else ""
    return "" if text.upper() == _REGISTRY_ABSENT else text


class OrganizationIdentity(BaseModel):
    """The identity block a facility shows in place of a person's.

    EPIC-006-F-007-S-001-REQ-B-002 names six things. The other
    organization name is conditional on the record carrying one, and its
    kind is a coded field rendered as its label, because a code shown raw
    tells the person nothing. The subpart's parent is named and never
    identified (REQ-B-003).

    The federal employer identification number and the parent
    organization's taxpayer identification number are absent by
    construction: this projection does not name them, which is the
    display-side enforcement of REQ-B-005.
    """

    legal_business_name: str = ""
    other_organization_name: str = ""
    other_organization_name_kind: str = ""
    npi: str = ""
    enumeration_date: str = ""
    status_active: bool = True
    is_subpart: bool = False
    parent_organization_name: str = ""

    # What the registry's coded other-name types mean. A code is shown as
    # its label so the person reads a kind of name rather than a number.
    # A table the class carries, not a field it holds.
    OTHER_NAME_KINDS: ClassVar[dict[str, str]] = {
        "1": "Former legal business name",
        "2": "Professional name",
        "3": "Doing business as",
        "4": "Former legal business name",
        "5": "Other name",
    }

    @classmethod
    def from_stored(cls, stored: dict) -> "OrganizationIdentity":
        active = stored.get("active") or []
        status_active = bool(active[-1].get("is_active", True)) if active else True
        kind_code = str(
            stored.get("provider_other_organization_name_type_code") or "").strip()
        subpart = str(stored.get("is_organization_subpart") or "").strip().upper()
        return cls(
            legal_business_name=stored.get(
                "provider_organization_name_legal_business_name", "") or "",
            other_organization_name=_present(
                stored.get("provider_other_organization_name")),
            other_organization_name_kind=cls.OTHER_NAME_KINDS.get(kind_code, ""),
            npi=stored.get("npi", "") or "",
            enumeration_date=stored.get("provider_enumeration_date", "") or "",
            status_active=status_active,
            is_subpart=subpart == "Y",
            parent_organization_name=_present(stored.get("parent_organization_lbn")),
        )


class AuthorizedOfficial(BaseModel):
    """The person the registry recorded as the facility's authorized
    official, per EPIC-006-F-007-S-002-REQ-B-001.

    Five fields. The middle name is conditional on the record carrying
    one; the other four are unconditional. The telephone number the record
    also carries is NOT here: B-001 names five fields, and this is a named
    person rather than a contact route the person is invited to use.
    """

    last_name: str = ""
    first_name: str = ""
    middle_name: str = ""
    title_or_position: str = ""
    credential: str = ""

    @classmethod
    def from_stored(cls, stored: dict) -> Optional["AuthorizedOfficial"]:
        official = cls(
            last_name=stored.get("authorized_official_last_name", "") or "",
            first_name=stored.get("authorized_official_first_name", "") or "",
            middle_name=stored.get("authorized_official_middle_name", "") or "",
            title_or_position=stored.get(
                "authorized_official_title_or_position", "") or "",
            credential=stored.get("authorized_official_credential", "") or "",
        )
        if not any((official.last_name, official.first_name,
                    official.title_or_position, official.credential)):
            return None
        return official


class FacilityKind(BaseModel):
    """One kind of facility the record carries, per
    EPIC-006-F-007-S-001-REQ-B-008.

    The classification label, the specialization where the record carries
    one, and the plain-language definition. All three are read from what
    the record holds; nothing is resolved while the panel paints, because
    a lookup in the click path is a second dependency and the panel must
    render when that dependency is unavailable.
    """

    code: str = ""
    classification: str = ""
    specialization: str = ""
    definition: str = ""

    @classmethod
    def from_stored(cls, stored: dict) -> list["FacilityKind"]:
        described = {
            (d.get("code") or "").strip(): d
            for d in (stored.get("facility_type_descriptions") or [])
            if isinstance(d, dict)
        }
        kinds: list["FacilityKind"] = []
        for taxonomy in stored.get("taxonomies") or []:
            if not isinstance(taxonomy, dict):
                continue
            code = (taxonomy.get("code") or "").strip()
            description = described.get(code, {})
            kinds.append(cls(
                code=code,
                classification=(taxonomy.get("code_label")
                                or description.get("classification_label") or ""),
                specialization=description.get("specialization_label") or "",
                definition=description.get("definition") or "",
            ))
        return kinds


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
    """One payer identifier the provider carries, per S-001-REQ-B-006.

    Four fields, so the person can see what the identifier is rather than
    infer it: the kind of coverage, the issuer, the state and the
    identifier itself. The registry records which payers issued an
    identifier, and nothing in that datum establishes that a plan's network
    includes the provider -- so this is never labelled as insurance
    accepted or as network membership.
    """
    coverage_kind: str = ""
    issuer: str = ""
    state: str = ""
    identifier: str = ""

    @classmethod
    def from_stored(cls, insurance_record: dict) -> "Insurance":
        return cls(
            coverage_kind=(insurance_record.get("insurance_type") or "").strip(),
            issuer=(insurance_record.get("payer_name") or "").strip(),
            state=(insurance_record.get("state") or "").strip(),
            identifier=(insurance_record.get("payer_id") or "").strip(),
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
    # The practice state for which no licensing authority could be
    # resolved. Empty when one was. Emitted in place of the destination so
    # a gap in the table surfaces rather than removing a link.
    unresolved_licensing_state: str = ""
    # Which kind of entity this panel is showing. The panel is the provider
    # panel with the identity block exchanged, so the renderer selects the
    # identity to paint from this rather than from the shape of the data.
    entity_type: str = "1"
    # Present on a facility, absent on a care giver.
    organization_identity: Optional[OrganizationIdentity] = None
    authorized_official: Optional[AuthorizedOfficial] = None
    facility_kinds: list[FacilityKind] = Field(default_factory=list)

    @classmethod
    def from_stored(
        cls,
        provider_name: str,
        npi: str,
        stored: Optional[dict],
        code_to_display: dict[str, str],
        primary_taxonomy_display: str,
        research_sites: dict[str, dict],
        unresolved_licensing_state: str = "",
    ) -> "ProviderDetailOutput":
        if stored is None:
            return cls(
                provider_name=provider_name,
                npi=npi,
                identity=Identity(npi=npi),
                research_sites={k: ResearchSite(**v) for k, v in research_sites.items()},
                unresolved_licensing_state=unresolved_licensing_state,
            )
        is_organization = str(stored.get("entity_type_code") or "1") == "2"
        return cls(
            provider_name=provider_name,
            npi=npi,
            unresolved_licensing_state=unresolved_licensing_state,
            entity_type="2" if is_organization else "1",
            organization_identity=(OrganizationIdentity.from_stored(stored)
                                   if is_organization else None),
            authorized_official=(AuthorizedOfficial.from_stored(stored)
                                 if is_organization else None),
            facility_kinds=(FacilityKind.from_stored(stored)
                            if is_organization else []),
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
