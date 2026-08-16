# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Schema-compliance test for provider records.

Purpose per operator directive 2026-07-31:
    "please create a pytest to ensure we are compliant to our own scheama.
    at this point the data is more trustworthy than the scheama but if we
    don't validate it is a discussion."

This test builds a provider record end-to-end using the same code path
the pipeline runs (build_provider_record + normalize + NUCC lookup),
then validates the result against the shipped ChatHealthyProvidersSchema.
A failure means either the code emits fields the schema does not
declare (data drift) or the schema declares fields the code does not
emit (schema drift). Either way it forces a design conversation and
prevents silent divergence.

We validate against ChatHealthyProvidersSchema.json (matches the record
shape actually shipped). ProviderRecordSchema.json is an aspirational
alternate that never matched real output and is not runtime-loaded by
anything -- skipped here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


_SCHEMA_PATH = (
    Path(__file__).resolve().parents[4]
    / "Website" / "schemas" / "ChatHealthyProvidersSchema.json"
)


@pytest.fixture
def providers_schema():
    with open(_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def sample_nucc_catalog():
    """Minimal NUCC catalog: two codes covering the shapes below."""
    return {
        "363L00000X": {"raw": {
            "Code": "363L00000X",
            "Display Name": "Nurse Practitioner",
            "Grouping": "Physician Assistants & Advanced Practice Nursing Providers",
            "Classification": "Nurse Practitioner",
        }},
        "207Q00000X": {"raw": {
            "Code": "207Q00000X",
            "Display Name": "Family Medicine Physician",
            "Grouping": "Allopathic & Osteopathic Physicians",
            "Classification": "Family Medicine",
        }},
    }


def _minimal_raw_row() -> dict:
    """A minimal NPPES-flavored raw row that exercises the label-bearing
    paths: one primary taxonomy, one license, practice + mailing
    addresses in different states, sex, is_sole_proprietor."""
    return {
        "NPI": "1962405589",
        "Entity Type Code": "1",
        "Provider Last Name (Legal Name)": "LONG",
        "Provider First Name": "SUZIE",
        "Provider Sex Code": "F",
        "Is Sole Proprietor": "Y",
        "Provider Credential Text": "C.R.N.P.",
        # Primary practice
        "Provider First Line Business Practice Location Address": "3411 SILVERSIDE RD",
        "Provider Business Practice Location Address City Name": "WILMINGTON",
        "Provider Business Practice Location Address State Name": "DE",
        "Provider Business Practice Location Address Postal Code": "19810",
        "Provider Business Practice Location Address Country Code (If outside U.S.)": "US",
        # Mailing
        "Provider First Line Business Mailing Address": "3411 SILVERSIDE RD",
        "Provider Business Mailing Address City Name": "WILMINGTON",
        "Provider Business Mailing Address State Name": "DE",
        "Provider Business Mailing Address Postal Code": "19810",
        "Provider Business Mailing Address Country Code (If outside U.S.)": "US",
        # Taxonomy (primary switch=Y)
        "Healthcare Provider Taxonomy Code_1": "363L00000X",
        "Healthcare Provider Primary Taxonomy Switch_1": "Y",
        # License
        "Provider License Number_1": "SP008386",
        "Provider License Number State Code_1": "PA",
    }


@pytest.mark.unit
def test_built_record_validates_against_providers_schema(
    providers_schema, sample_nucc_catalog
):
    """Build a normal record with build_provider_record + validate."""
    import jsonschema
    from provider_record_builder import build_provider_record

    raw = _minimal_raw_row()
    doc = build_provider_record(
        raw, npi=raw["NPI"], run_id="test-run",
        nucc_catalog=sample_nucc_catalog,
    )
    # loaded_at is stamped by the dispatcher in production; add here so
    # the schema's required-list is satisfied.
    doc.setdefault("loaded_at", "2026-07-31T18:00:00Z")

    # Sanity: the new-shape fields we're forcing in this commit
    assert doc.get("primary_taxonomy_code") == "363L00000X"
    assert doc.get("is_primary_care") is True
    # build_provider_record stamps reference_status='current_reference'
    # when the code resolves via the NUCC catalog; the schema's
    # conditional-required allOf branch on taxonomies items depends on
    # this to skip requiring grouping/classification/specialization/definition.
    assert doc["taxonomies"] == [{
        "code": "363L00000X",
        "code_label": "Nurse Practitioner",
        "reference_status": "current_reference",
    }]
    assert "classification" not in (doc["taxonomies"][0])
    assert doc["licenses"][0]["state"] == "PA"
    assert doc["licenses"][0]["state_label"] == "Pennsylvania"
    # Address labels
    for addr in ([doc["business_address"]] if doc.get("business_address") else []) + (doc.get("practice_addresses") or []):
        if addr.get("state"):
            assert "state_label" in addr, f"state_label missing on {addr!r}"
        if addr.get("country"):
            assert "country_code_label" in addr, f"country_code_label missing on {addr!r}"
    assert doc.get("provider_sex_code_label") == "Female"
    assert doc.get("is_sole_proprietor_label") == "Yes"

    # Full-schema validation. Wrap into the envelope shape the schema
    # expects: providers = list of provider objects. The schema top-level
    # has a "providers" property.
    envelope = {
        "$schema": "https://dev.chathealthy.ai/schemas/ChatHealthyProvidersSchema.json",
        "Providers": {"provider": [doc]},
    }
    validator = jsonschema.Draft202012Validator(providers_schema)
    errors = sorted(validator.iter_errors(envelope), key=lambda e: e.path)
    assert not errors, (
        "Provider record failed ChatHealthyProvidersSchema validation. "
        "This is a real discussion: either the code emits fields not "
        "declared, or the schema declares fields the code does not emit.\n"
        + "\n".join(
            f"  path={list(e.absolute_path)} msg={e.message}" for e in errors[:10]
        )
    )
