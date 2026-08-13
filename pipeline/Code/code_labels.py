# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Coded-value -> human-readable-label lookups.

LLD v39 section 7.1 (records shape): "Human-readable code labels --
every coded field on the record gets a sibling <field>_label with the
human-readable text."

This module is the single source of truth for those label maps + the
tiny helpers that consult them.

Labels are populated when the code is present. A code with no map entry
is a data-quality problem, not a fallback opportunity: label lookup
returns the raw code so downstream renderers show SOMETHING rather than
silently dropping, but the pipeline emits a discrepancy for the missing
mapping so we surface it in the reconciliation report.
"""

from __future__ import annotations


# ── US state / territory two-letter code -> full name ────────────────────────
US_STATE_LABELS: dict[str, str] = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas",
    "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida",
    "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky",
    "LA": "Louisiana", "ME": "Maine", "MD": "Maryland",
    "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio",
    "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania",
    "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont",
    "VA": "Virginia", "WA": "Washington", "WV": "West Virginia",
    "WI": "Wisconsin", "WY": "Wyoming",
    # Territories and armed-forces (present in NPPES data)
    "PR": "Puerto Rico", "VI": "U.S. Virgin Islands", "GU": "Guam",
    "AS": "American Samoa", "MP": "Northern Mariana Islands",
    "FM": "Federated States of Micronesia", "MH": "Marshall Islands",
    "PW": "Palau",
    "AA": "Armed Forces Americas", "AE": "Armed Forces Europe",
    "AP": "Armed Forces Pacific",
}

# ── Country code -> full name ────────────────────────────────────────────────
# ISO 3166-1 alpha-2. NPPES publishes country as 2-letter code; the vast
# majority of records are US. Cover the common non-US countries providers
# use plus a few dozen more; a code with no map entry falls through to
# the code itself (documented in state_label_of).
COUNTRY_LABELS: dict[str, str] = {
    "US": "United States", "CA": "Canada", "MX": "Mexico",
    "GB": "United Kingdom", "IE": "Ireland", "FR": "France",
    "DE": "Germany", "ES": "Spain", "IT": "Italy", "NL": "Netherlands",
    "BE": "Belgium", "CH": "Switzerland", "AT": "Austria",
    "SE": "Sweden", "NO": "Norway", "DK": "Denmark", "FI": "Finland",
    "PL": "Poland", "CZ": "Czechia", "PT": "Portugal", "GR": "Greece",
    "IS": "Iceland", "LU": "Luxembourg",
    "AU": "Australia", "NZ": "New Zealand",
    "JP": "Japan", "KR": "South Korea", "CN": "China", "IN": "India",
    "PH": "Philippines", "SG": "Singapore", "HK": "Hong Kong",
    "TW": "Taiwan", "TH": "Thailand", "VN": "Vietnam",
    "IL": "Israel", "AE": "United Arab Emirates", "SA": "Saudi Arabia",
    "TR": "Turkey", "EG": "Egypt", "ZA": "South Africa",
    "BR": "Brazil", "AR": "Argentina", "CL": "Chile", "CO": "Colombia",
    "PE": "Peru", "VE": "Venezuela",
    "RU": "Russia", "UA": "Ukraine",
}

# ── NPPES sex code -> full name ──────────────────────────────────────────────
# NPPES "Provider Sex Code" column values.
SEX_CODE_LABELS: dict[str, str] = {
    "M": "Male",
    "F": "Female",
    "U": "Unknown",
}

# ── NPPES Y/N boolean-flavored code -> full name ─────────────────────────────
# For fields like "Is Sole Proprietor" / "Is Organization Subpart" that
# carry 'Y' / 'N' / 'X' (or blank). Not to be confused with the taxonomy
# primary switch (which is a per-slot column encoding a per-provider fact
# and is handled explicitly in provider_record_builder).
# NPPES Data Dissemination code values, Sole Proprietor Codes:
#   Y = Yes, Entity Type 1 Provider (Individual) is a Sole Proprietor
#   N = No,  Entity Type 1 Provider (Individual) is not a Sole Proprietor
#   X = Not Answered  (rendered to users as "Unknown")
# https://www.cms.gov/regulations-and-guidance/administrative-simplification
#   /nationalprovidentstand/downloads/data_dissemination_file-code_values.pdf
# X was labelled "Not Applicable" until 2026-08-13, which told 586 individuals
# a question they had simply not answered did not apply to them.
YES_NO_LABELS: dict[str, str] = {
    "Y": "Yes",
    "N": "No",
    "X": "Unknown",
}


def state_label_of(state_code: str | None) -> str | None:
    """Return the full US state name for a two-letter code. None -> None.
    Unknown code -> the code itself (so downstream renderers show
    SOMETHING rather than dropping the value silently)."""
    if state_code is None:
        return None
    code = str(state_code).strip().upper()
    if not code:
        return None
    return US_STATE_LABELS.get(code, code)


def country_label_of(country_code: str | None) -> str | None:
    """Same shape as state_label_of."""
    if country_code is None:
        return None
    code = str(country_code).strip().upper()
    if not code:
        return None
    return COUNTRY_LABELS.get(code, code)


def sex_label_of(sex_code: str | None) -> str | None:
    if sex_code is None:
        return None
    code = str(sex_code).strip().upper()
    if not code:
        return None
    return SEX_CODE_LABELS.get(code, code)


def yes_no_label_of(yn_code: str | None) -> str | None:
    if yn_code is None:
        return None
    code = str(yn_code).strip().upper()
    if not code:
        return None
    return YES_NO_LABELS.get(code, code)
