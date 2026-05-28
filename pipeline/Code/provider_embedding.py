# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).

"""provider_embedding — canonical embedding projection for provider records.

One embedding. Same projection for all users (consumer search + network manager).
Trust and status signals are explicit in the text; the model does not infer them.

Public API
----------
    should_embed(record)  -> bool      — filter before projecting
    project(record)       -> dict      — derive projection from raw record
    render(projection)    -> str       — render projection to embedding text

Usage
-----
    from provider_embedding import should_embed, project, render

    if should_embed(record):
        text = render(project(record))
        # pass text to embedding model
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# County source → trust tier
# ---------------------------------------------------------------------------

_TRUST_TIER: dict[str, str] = {
    "crosswalk_pass1":          "high",
    "crosswalk_zip_fixed":      "high",
    "geocoder_pass2_batch":     "medium",
    "geocoder_pass3_billing":   "medium",
    "geocoder_pass4_maps":      "medium",
    "geocoder_pass5_maps_billing": "medium",
    "geocoder_pass6_nppes":     "medium",
    # Per-pass failure labels — the address went into the funnel and the
    # named pass gave up. All four collapse to "low" trust for the embedding
    # consumer, but the distinct labels remain so reports and recovery can
    # see where the funnel stopped.
    "geocoder_pass2_failed":    "low",
    "geocoder_pass3_failed":    "low",
    "geocoder_pass4_failed":    "low",
    "geocoder_pass6_failed":    "low",
    "out_of_scope":             "excluded",
}

# Every failure-tag the funnel can write. Used by callers that need to
# answer the question "did county enrichment give up on this address?"
# without enumerating each pass label.
_FAILED_SOURCES: frozenset[str] = frozenset({
    "geocoder_pass2_failed",
    "geocoder_pass3_failed",
    "geocoder_pass4_failed",
    "geocoder_pass6_failed",
})

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PLACEHOLDER_VALUES = frozenset({"<UNAVAIL>", "N/A", "NONE", "NULL"})


def _clean(value: Any) -> str | None:
    """Return stripped string, or None for blank/placeholder values."""
    if value is None:
        return None
    s = str(value).strip()
    return None if (not s or s.upper() in _PLACEHOLDER_VALUES) else s


def _practice_addresses(record: dict) -> list:
    """Return practice-type entries from addresses[].

    Post-schema-reconciliation: practice_address + mailing_address are unified
    into addresses[] with an address_type discriminator. Practice locations are
    every entry whose address_type == "practice".
    """
    return [
        a for a in (record.get("addresses") or [])
        if isinstance(a, dict) and a.get("address_type") == "practice"
    ]


def _business_address(record: dict) -> dict | None:
    """Return the (single) business-type entry from addresses[], or None."""
    for a in (record.get("addresses") or []):
        if isinstance(a, dict) and a.get("address_type") == "business":
            return a
    return None


def _is_active(record: dict) -> bool:
    """True if the provider is currently active per the `active` event log.

    Missing/empty `active` → True (default). Otherwise the last event's
    is_active value is the current state.
    """
    events = record.get("active") or []
    if not events:
        return True
    last = events[-1]
    if not isinstance(last, dict):
        return True
    return bool(last.get("is_active", True))


def _format_address(addr: dict | None) -> str | None:
    if not addr:
        return None
    line1   = _clean(addr.get("line1"))
    line2   = _clean(addr.get("line2"))
    city    = _clean(addr.get("city"))
    state   = _clean(addr.get("state"))
    zip_    = _clean(addr.get("zip"))

    parts: list[str] = []
    if line1:
        parts.append(line1)
    if line2:
        parts.append(line2)

    locality = ", ".join(p for p in [city, state] if p)
    if zip_:
        locality = f"{locality} {zip_}" if locality else zip_
    if locality:
        parts.append(locality)

    return ", ".join(parts) if parts else None


def _county_resolution_status(county: dict) -> str:
    if county.get("fips"):
        return "resolved"
    if county.get("source") in _FAILED_SOURCES:
        return "unresolved"
    return "excluded"


def _county_source_summary(county: dict) -> str:
    source = county.get("source", "")
    mapping = {
        "crosswalk_pass1":          lambda c: (
            f"ZIP crosswalk (res_ratio: {c['zip_ratio']})"
            if c.get("zip_ratio") is not None else "ZIP crosswalk"
        ),
        "crosswalk_zip_fixed":      lambda _: "ZIP crosswalk (corrected ZIP/state)",
        "geocoder_pass2_batch":     lambda _: "Census geocoder — practice address",
        "geocoder_pass3_billing":   lambda _: "Census geocoder — billing address",
        "geocoder_pass4_maps":      lambda _: "Google Maps — practice address",
        "geocoder_pass5_maps_billing": lambda _: "Google Maps — billing address",
        "geocoder_pass6_nppes":     lambda _: "NPPES registry lookup",
        "geocoder_pass2_failed":    lambda _: "unresolved — Pass 2 (Census batch on practice address) gave up",
        "geocoder_pass3_failed":    lambda _: "unresolved — Pass 3 (Census batch on billing address) gave up",
        "geocoder_pass4_failed":    lambda _: "unresolved — Pass 4 (Google Maps on practice address) gave up",
        "geocoder_pass6_failed":    lambda _: "unresolved — Pass 6 (NPPES last resort) gave up; funnel exhausted",
        "out_of_scope":             lambda c: f"excluded ({c.get('reason', 'unknown')})",
    }
    fn = mapping.get(source)
    return fn(county) if fn else source


def _display_name_individual(record: dict) -> str | None:
    parts = [
        _clean(record.get("provider_name_prefix_text")),
        _clean(record.get("provider_first_name")),
        _clean(record.get("provider_middle_name")),
        _clean(record.get("provider_last_name_legal_name")),
        _clean(record.get("provider_name_suffix_text")),
    ]
    name = " ".join(p for p in parts if p)
    credential = _clean(record.get("provider_credential_text"))
    if credential:
        name = f"{name}, {credential}"
    return name or None


def _format_licenses(licenses: list) -> str | None:
    parts = []
    for lic in licenses:
        state  = _clean(lic.get("state"))
        number = _clean(lic.get("number"))
        if state and number:
            parts.append(f"{state} #{number}")
        elif state:
            parts.append(state)
    return ", ".join(parts) if parts else None


def _format_other_identifiers(entries: list) -> str | None:
    """Format the addresses[]-shape other_identifiers (list of dicts).

    Each entry: {identifier, type, state, issuer}. Post-schema-reconciliation
    other_identifiers is an array of objects (not the legacy parallel arrays).
    """
    parts = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        id_clean = _clean(entry.get("identifier"))
        if not id_clean:
            continue
        type_val   = _clean(entry.get("type"))
        state_val  = _clean(entry.get("state"))
        issuer_val = _clean(entry.get("issuer"))
        meta = ", ".join(p for p in [type_val, state_val, issuer_val] if p)
        parts.append(f"{id_clean} ({meta})" if meta else id_clean)
    return "; ".join(parts) if parts else None


def _format_authorized_official(record: dict) -> str | None:
    first      = _clean(record.get("authorized_official_first_name"))
    middle     = _clean(record.get("authorized_official_middle_name"))
    last       = _clean(record.get("authorized_official_last_name"))
    title      = _clean(record.get("authorized_official_title_or_position"))
    credential = _clean(record.get("authorized_official_credential_text"))

    name = " ".join(p for p in [first, middle, last] if p)
    if not name:
        return None
    if credential:
        name = f"{name}, {credential}"
    if title:
        name = f"{name} — {title}"
    return name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def should_embed(record: dict) -> bool:
    """Return False for records that must never be embedded.

    Excluded: any record with the bad_data flag set. The two reasons that
    flag fires for (no_address, zip_state_mismatch) both leave the record
    without a coherent address, which makes the embedding text meaningless
    for similarity search.
    """
    bad_data = record.get("bad_data") or {}
    return not bad_data.get("flagged", False)


def project(record: dict) -> dict:
    """Derive the canonical embedding projection from a raw provider record.

    Returns a flat dict containing all fields needed by render().
    System fields (_id, load_id, record_id, worker_id) are excluded.
    Blank and placeholder values are normalised to None.
    """
    # The primary practice address's county describes the provider's main
    # location for embedding purposes. Post-schema-reconciliation, county
    # is always nested under each addresses[] element — no top-level fallback.
    _primary_practice = next(iter(_practice_addresses(record)), {})
    county = _primary_practice.get("county") if isinstance(_primary_practice.get("county"), dict) else {}
    is_indiv = record.get("entity_type_code") == "1"

    # --- derived: name ---
    display_name = (
        _display_name_individual(record) if is_indiv
        else _clean(record.get("provider_organization_name_legal_business_name"))
    )

    # --- derived: taxonomies ---
    taxonomies = record.get("taxonomies") or []
    primary_taxonomy_code = next(
        (_clean(t.get("code")) for t in taxonomies if t.get("primary")), None
    )
    all_taxonomy_codes = " ".join(
        _clean(t.get("code")) for t in taxonomies if _clean(t.get("code"))
    ) or None

    # --- derived: addresses ---
    # Format every practice-type addresses[] entry so the embedding text
    # reflects every location a multi-site provider works at (not just the
    # primary). The business-type entry feeds the mailing/billing line.
    practice_addrs = [
        s for s in (_format_address(a) for a in _practice_addresses(record)) if s
    ]
    practice_addr = " | ".join(practice_addrs) if practice_addrs else None
    mailing_addr  = _format_address(_business_address(record))
    if mailing_addr and mailing_addr in practice_addrs:   # omit if duplicate of any practice
        mailing_addr = None

    # --- derived: county ---
    county_resolution_status = _county_resolution_status(county)
    county_trust_tier        = _TRUST_TIER.get(county.get("source", ""), "low")
    county_source_summary    = _county_source_summary(county)

    # --- derived: NPI status ---
    # Driven by the `active` event log (replaces npi_deactivation_date /
    # npi_reactivation_date scalars per the schema reconciliation).
    npi_status = "active" if _is_active(record) else "inactive"

    # --- derived: flags ---
    # The provider is foreign if ANY practice-type addresses[] entry has a
    # non-US country code (matches the Pass-1 out-of-scope semantics).
    foreign_provider = "no"
    for _addr in _practice_addresses(record):
        _country = _addr.get("country")
        if _country and _country.upper() not in ("US", ""):
            foreign_provider = "yes"
            break

    sole_prop = _clean(record.get("is_sole_proprietor"))
    sole_prop = ("yes" if sole_prop == "Y" else "no") if sole_prop else None

    org_subpart = _clean(record.get("is_organization_subpart"))
    org_subpart = ("yes" if org_subpart == "Y" else "no") if org_subpart else None

    # --- derived: prescriber status + drugs ---
    # F5 (2026-05-22): can_prescribe is now a plain boolean stamped from the
    # proprietary NUCC classification catalog. The legacy nested-dict shape
    # (with .flagged / .cost_measures / .drugs from the prescriber pipeline)
    # is EvaluateCare territory; this code handles both shapes during the
    # transition.
    can_prescribe_raw = record.get("can_prescribe")
    if isinstance(can_prescribe_raw, bool):
        can_prescribe_flag = "yes" if can_prescribe_raw else "no"
        can_prescribe_data = {}
    elif isinstance(can_prescribe_raw, dict):
        can_prescribe_data = can_prescribe_raw
        can_prescribe_flag = "yes" if can_prescribe_data.get("flagged") else "no"
    else:
        can_prescribe_flag = "no"
        can_prescribe_data = {}
    prescriber_drugs_text = None
    generic_ratio_band = None
    on_label_band = None
    specialty_generic_percentile = None
    if can_prescribe_data.get("flagged"):
        cost_measures = can_prescribe_data.get("cost_measures") or {}
        generic_ratio_band = cost_measures.get("generic_ratio_band")
        on_label_band = cost_measures.get("on_label_band")
        specialty_generic_percentile = cost_measures.get("specialty_generic_percentile")

        # Build embedding text from pathology tree (preferred) or flat drugs
        pathologies = cost_measures.get("pathologies", [])
        if pathologies:
            # Pathology-first: indication → ICD-10 → molecules
            parts = []
            for path in pathologies:
                ind = path.get("indication", "")
                codes = path.get("icd10_codes", [])
                if ind:
                    parts.append(f"{ind} ({', '.join(codes)})" if codes else ind)
                for mol in path.get("molecules", []):
                    parts.append(mol.get("molecule", ""))
            prescriber_drugs_text = "; ".join(p for p in parts if p) or None
        elif can_prescribe_data.get("drugs"):
            # Fallback: flat drug list (before pathology tree is built)
            drug_parts = []
            for drug in can_prescribe_data["drugs"]:
                molecule = drug.get("molecule", "")
                if molecule:
                    drug_parts.append(molecule)
                for ind in drug.get("indications", []):
                    indication = ind.get("indication", "")
                    if indication:
                        drug_parts.append(indication)
                    for code in ind.get("icd10_codes", []):
                        drug_parts.append(code)
            prescriber_drugs_text = "; ".join(drug_parts) if drug_parts else None

    return {
        # --- identity ---
        "entity_type_label":        "individual" if is_indiv else "organization",
        "display_name":             display_name,
        "npi":                      _clean(record.get("npi")),
        "npi_status":               npi_status,
        # --- individual fields ---
        "credential":               _clean(record.get("provider_credential_text")) if is_indiv else None,
        "sex":                      _clean(record.get("provider_sex_code"))         if is_indiv else None,
        "sole_proprietor":          sole_prop                                        if is_indiv else None,
        # --- organization fields ---
        "organization_name":        _clean(record.get("provider_organization_name_legal_business_name"))   if not is_indiv else None,
        "other_organization_name":  _clean(record.get("provider_other_organization_name"))                 if not is_indiv else None,
        "other_organization_name_type": _clean(record.get("provider_other_organization_name_type_code"))   if not is_indiv else None,
        "is_organization_subpart":  org_subpart                                                             if not is_indiv else None,
        "parent_organization_name": _clean(record.get("parent_organization_lbn"))                          if not is_indiv else None,
        "parent_organization_tin":  _clean(record.get("parent_organization_tin"))                          if not is_indiv else None,
        "authorized_official":      _format_authorized_official(record)                                    if not is_indiv else None,
        "ein":                      _clean(record.get("employer_identification_number_ein"))               if not is_indiv else None,
        # --- taxonomies ---
        "primary_taxonomy_code":    primary_taxonomy_code,
        "all_taxonomy_codes":       all_taxonomy_codes,
        # --- addresses ---
        "practice_address":         practice_addr,
        "mailing_address":          mailing_addr,
        # --- county ---
        "county_name":              _clean(county.get("name")),
        "county_fips":              _clean(str(county["fips"])) if county.get("fips") else None,
        "county_resolution_status": county_resolution_status,
        "county_trust_tier":        county_trust_tier,
        "county_source_summary":    county_source_summary,
        "county_reason":            _clean(county.get("reason")),
        # --- flags ---
        "foreign_provider":         foreign_provider,
        # --- licenses & identifiers ---
        "licenses":                 _format_licenses(record.get("licenses") or []),
        "other_identifiers":        _format_other_identifiers(record.get("other_identifiers")),
        # --- dates ---
        "enumeration_date":         _clean(record.get("provider_enumeration_date")),
        "last_update_date":         _clean(record.get("last_update_date")),
        "certification_date":       _clean(record.get("certification_date")),
        # --- prescriber ---
        "can_prescribe":            can_prescribe_flag,
        "generic_ratio_band":       generic_ratio_band,
        "on_label_band":            on_label_band,
        "specialty_generic_percentile": specialty_generic_percentile,
        "prescriber_drugs":         prescriber_drugs_text,
    }


# Stable field order for rendering — one list per entity type.
# Fields absent from the projection (None) are silently omitted.
_INDIVIDUAL_FIELDS = [
    ("record_type",               "provider"),
    ("entity_type",               "entity_type_label"),
    ("display_name",              "display_name"),
    ("npi",                       "npi"),
    ("npi_status",                "npi_status"),
    ("credential",                "credential"),
    ("sex",                       "sex"),
    ("sole_proprietor",           "sole_proprietor"),
    ("primary_taxonomy_code",     "primary_taxonomy_code"),
    ("all_taxonomy_codes",        "all_taxonomy_codes"),
    ("practice_address",          "practice_address"),
    ("mailing_address",           "mailing_address"),
    ("county_name",               "county_name"),
    ("county_fips",               "county_fips"),
    ("foreign_provider",          "foreign_provider"),
    ("licenses",                  "licenses"),
    ("other_identifiers",         "other_identifiers"),
    ("enumeration_date",          "enumeration_date"),
    ("last_update_date",          "last_update_date"),
    ("certification_date",        "certification_date"),
    ("can_prescribe",             "can_prescribe"),
    ("generic_ratio_band",        "generic_ratio_band"),
    ("on_label_band",             "on_label_band"),
    ("specialty_generic_percentile", "specialty_generic_percentile"),
    ("prescriber_drugs",          "prescriber_drugs"),
]

_ORGANIZATION_FIELDS = [
    ("record_type",               "provider"),
    ("entity_type",               "entity_type_label"),
    ("display_name",              "display_name"),
    ("organization_name",         "organization_name"),
    ("other_organization_name",   "other_organization_name"),
    ("other_organization_name_type", "other_organization_name_type"),
    ("npi",                       "npi"),
    ("npi_status",                "npi_status"),
    ("ein",                       "ein"),
    ("is_organization_subpart",   "is_organization_subpart"),
    ("parent_organization_name",  "parent_organization_name"),
    ("parent_organization_tin",   "parent_organization_tin"),
    ("authorized_official",       "authorized_official"),
    ("primary_taxonomy_code",     "primary_taxonomy_code"),
    ("all_taxonomy_codes",        "all_taxonomy_codes"),
    ("practice_address",          "practice_address"),
    ("mailing_address",           "mailing_address"),
    ("county_name",               "county_name"),
    ("county_fips",               "county_fips"),
    ("foreign_provider",          "foreign_provider"),
    ("licenses",                  "licenses"),
    ("other_identifiers",         "other_identifiers"),
    ("enumeration_date",          "enumeration_date"),
    ("last_update_date",          "last_update_date"),
    ("certification_date",        "certification_date"),
    ("can_prescribe",             "can_prescribe"),
    ("prescriber_drugs",          "prescriber_drugs"),
]


def render(projection: dict) -> str:
    """Render a projection dict to embedding text.

    Stable field order. None values are omitted. No blank lines.
    """
    fields = (
        _INDIVIDUAL_FIELDS
        if projection.get("entity_type_label") == "individual"
        else _ORGANIZATION_FIELDS
    )
    lines = []
    for label, key in fields:
        # Fixed literals (e.g. record_type: provider) use label as both key and value
        value = key if label == "record_type" else projection.get(key)
        if value is not None:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
