# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for provider_embedding — projection and rendering."""

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from provider_embedding import should_embed, project, render


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

INDIVIDUAL_ENRICHED = {
    "npi": "1144247073",
    "entity_type_code": "1",
    "provider_name_prefix_text": "Dr.",
    "provider_first_name": "Jane",
    "provider_middle_name": "A.",
    "provider_last_name_legal_name": "Smith",
    "provider_credential_text": "M.D.",
    "provider_sex_code": "F",
    "is_sole_proprietor": "N",
    "taxonomies": [
        {"code": "207R00000X", "primary": True},
        {"code": "207RC0000X", "primary": False},
    ],
    "licenses": [
        {"state": "GA", "number": "G12345"},
        {"state": "FL", "number": "F67890"},
    ],
    "practice_address": {
        "line1": "123 Peachtree St",
        "line2": "Suite 400",
        "city": "Atlanta",
        "state": "GA",
        "zip": "30301",
        "country": "US",
        "phone": "4045551234",
    },
    "mailing_address": {
        "line1": "PO Box 999",
        "city": "Atlanta",
        "state": "GA",
        "zip": "30302",
    },
    "county": {
        "fips": "13121",
        "name": "Fulton County",
        "source": "crosswalk_pass1",
        "zip_ratio": 0.99,
    },
    "other_identifiers": ["G123", "M456"],
    "other_identifier_types": ["04", "05"],
    "other_identifier_states": ["GA", "GA"],
    "other_identifier_issuers": [],
    "provider_enumeration_date": "07/16/2006",
    "last_update_date": "01/15/2024",
    "certification_date": "01/15/2024",
    # system fields — must not appear in output
    "_id": "mongo_object_id",
    "load_id": "ea36237b-377c-4888-8fa9-5faab71e4820:0",
    "record_id": 1,
    "worker_id": 0,
}

ORGANIZATION_ENRICHED = {
    "npi": "1215954557",
    "entity_type_code": "2",
    "provider_organization_name_legal_business_name": "MAYO CLINIC",
    "provider_other_organization_name": "Mayo Foundation",
    "provider_other_organization_name_type_code": "5",
    "employer_identification_number_ein": "41-6011702",
    "is_organization_subpart": "Y",
    "parent_organization_lbn": "Mayo Foundation for Medical Education and Research",
    "parent_organization_tin": "41-6011702",
    "authorized_official_last_name": "Johnson",
    "authorized_official_first_name": "Robert",
    "authorized_official_middle_name": "T.",
    "authorized_official_title_or_position": "CEO",
    "authorized_official_credential_text": "M.D.",
    "taxonomies": [{"code": "282N00000X", "primary": True}],
    "licenses": [{"state": "MN", "number": "MN001"}],
    "practice_address": {
        "line1": "200 First St SW",
        "city": "Rochester",
        "state": "MN",
        "zip": "55905",
        "country": "US",
    },
    "mailing_address": {
        "line1": "200 First St SW",
        "city": "Rochester",
        "state": "MN",
        "zip": "55905",
        "country": "US",
    },
    "county": {
        "fips": "27109",
        "name": "Olmsted County",
        "source": "geocoder_pass2_batch",
    },
    "provider_enumeration_date": "04/01/2005",
    "last_update_date": "03/01/2024",
    "_id": "mongo_object_id",
    "load_id": "ea36237b-377c-4888-8fa9-5faab71e4820:1",
    "record_id": 2,
    "worker_id": 0,
}

INDIVIDUAL_GEOCODER_FAILED = {
    "npi": "1234567890",
    "entity_type_code": "1",
    "provider_first_name": "Carlos",
    "provider_last_name_legal_name": "Rivera",
    "provider_credential_text": "D.O.",
    "taxonomies": [{"code": "207P00000X", "primary": True}],
    "practice_address": {
        "line1": "456 Rural Rd",
        "city": "Smalltown",
        "state": "TX",
        "zip": "79901",
        "country": "US",
    },
    "county": {
        "fips": None,
        "source": "geocoder_failed",
    },
}

INDIVIDUAL_FOREIGN = {
    "npi": "1987654321",
    "entity_type_code": "1",
    "provider_first_name": "Pierre",
    "provider_last_name_legal_name": "Dubois",
    "taxonomies": [{"code": "207R00000X", "primary": True}],
    "practice_address": {
        "line1": "12 Rue de Paris",
        "city": "Paris",
        "state": "FR",
        "zip": "75001",
        "country": "FR",
    },
    "county": {
        "fips": None,
        "source": "out_of_scope",
        "reason": "foreign_provider",
    },
}

INDIVIDUAL_NO_ADDRESS = {
    "npi": "1111111111",
    "entity_type_code": "1",
    "provider_first_name": "Ghost",
    "provider_last_name_legal_name": "Provider",
    "county": {
        "fips": None,
        "source": "out_of_scope",
        "reason": "no_address",
    },
}

INDIVIDUAL_ZIP_STATE_MISMATCH = {
    "npi": "2222222222",
    "entity_type_code": "1",
    "provider_first_name": "Bad",
    "provider_last_name_legal_name": "Address",
    "practice_address": {
        "line1": "789 Confused Ave",
        "city": "Somewhere",
        "state": "CA",
        "zip": "30301",  # GA zip in CA — mismatch
        "country": "US",
    },
    "county": {
        "fips": None,
        "source": "out_of_scope",
        "reason": "zip_state_mismatch",
    },
}

INDIVIDUAL_INACTIVE = {
    "npi": "3333333333",
    "entity_type_code": "1",
    "provider_first_name": "Former",
    "provider_last_name_legal_name": "Doctor",
    "npi_deactivation_date": "06/01/2020",
    "taxonomies": [{"code": "207R00000X", "primary": True}],
    "practice_address": {
        "line1": "100 Old Clinic Rd",
        "city": "Nashville",
        "state": "TN",
        "zip": "37201",
        "country": "US",
    },
    "county": {
        "fips": "47037",
        "name": "Davidson County",
        "source": "crosswalk_pass1",
        "zip_ratio": 0.98,
    },
}


# ---------------------------------------------------------------------------
# should_embed tests
# ---------------------------------------------------------------------------

class TestShouldEmbed:

    def test_enriched_individual_embeds(self):
        assert should_embed(INDIVIDUAL_ENRICHED) is True

    def test_enriched_organization_embeds(self):
        assert should_embed(ORGANIZATION_ENRICHED) is True

    def test_geocoder_failed_embeds(self):
        assert should_embed(INDIVIDUAL_GEOCODER_FAILED) is True

    def test_foreign_provider_embeds(self):
        assert should_embed(INDIVIDUAL_FOREIGN) is True

    def test_inactive_npi_embeds(self):
        assert should_embed(INDIVIDUAL_INACTIVE) is True

    def test_no_address_excluded(self):
        assert should_embed(INDIVIDUAL_NO_ADDRESS) is False

    def test_zip_state_mismatch_excluded(self):
        assert should_embed(INDIVIDUAL_ZIP_STATE_MISMATCH) is False


# ---------------------------------------------------------------------------
# project() tests
# ---------------------------------------------------------------------------

class TestProject:

    def test_individual_system_fields_excluded(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert "_id" not in p
        assert "load_id" not in p
        assert "record_id" not in p
        assert "worker_id" not in p

    def test_individual_display_name(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["display_name"] == "Dr. Jane A. Smith, M.D."

    def test_individual_entity_type_label(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["entity_type_label"] == "individual"

    def test_individual_npi_status_active(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["npi_status"] == "active"

    def test_individual_npi_status_inactive(self):
        p = project(INDIVIDUAL_INACTIVE)
        assert p["npi_status"] == "inactive"

    def test_individual_primary_taxonomy(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["primary_taxonomy_code"] == "207R00000X"

    def test_individual_all_taxonomies(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert "207R00000X" in p["all_taxonomy_codes"]
        assert "207RC0000X" in p["all_taxonomy_codes"]

    def test_individual_mailing_omitted_when_same_as_practice(self):
        p = project(ORGANIZATION_ENRICHED)  # org has identical addresses
        assert p["mailing_address"] is None

    def test_individual_mailing_included_when_different(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["mailing_address"] is not None
        assert "PO Box" in p["mailing_address"]

    def test_individual_county_high_trust(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["county_trust_tier"] == "high"
        assert p["county_resolution_status"] == "resolved"

    def test_geocoder_failed_county_low_trust(self):
        p = project(INDIVIDUAL_GEOCODER_FAILED)
        assert p["county_trust_tier"] == "low"
        assert p["county_resolution_status"] == "unresolved"
        assert p["county_fips"] is None

    def test_foreign_provider_flag(self):
        p = project(INDIVIDUAL_FOREIGN)
        assert p["foreign_provider"] == "yes"

    def test_domestic_provider_flag(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["foreign_provider"] == "no"

    def test_sole_proprietor_mapped(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["sole_proprietor"] == "no"

    def test_org_fields_absent_on_individual(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["organization_name"] is None
        assert p["authorized_official"] is None
        assert p["ein"] is None

    def test_individual_fields_absent_on_org(self):
        p = project(ORGANIZATION_ENRICHED)
        assert p["credential"] is None
        assert p["sex"] is None
        assert p["sole_proprietor"] is None

    def test_org_display_name(self):
        p = project(ORGANIZATION_ENRICHED)
        assert p["display_name"] == "MAYO CLINIC"

    def test_org_authorized_official(self):
        p = project(ORGANIZATION_ENRICHED)
        assert p["authorized_official"] == "Robert T. Johnson, M.D. — CEO"

    def test_org_subpart_flag_mapped(self):
        p = project(ORGANIZATION_ENRICHED)
        assert p["is_organization_subpart"] == "yes"

    def test_org_medium_trust(self):
        p = project(ORGANIZATION_ENRICHED)
        assert p["county_trust_tier"] == "medium"

    def test_licenses_formatted(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert p["licenses"] == "GA #G12345, FL #F67890"

    def test_placeholder_stripped(self):
        record = {**INDIVIDUAL_ENRICHED, "provider_credential_text": "<UNAVAIL>"}
        p = project(record)
        assert p["credential"] is None

    def test_county_source_summary_crosswalk(self):
        p = project(INDIVIDUAL_ENRICHED)
        assert "ZIP crosswalk" in p["county_source_summary"]
        assert "0.99" in p["county_source_summary"]


# ---------------------------------------------------------------------------
# render() tests
# ---------------------------------------------------------------------------

class TestRender:

    def test_individual_renders_record_type(self):
        text = render(project(INDIVIDUAL_ENRICHED))
        assert "record_type: provider" in text

    def test_individual_renders_entity_type(self):
        text = render(project(INDIVIDUAL_ENRICHED))
        assert "entity_type: individual" in text

    def test_individual_renders_npi(self):
        text = render(project(INDIVIDUAL_ENRICHED))
        assert "npi: 1144247073" in text

    def test_individual_no_blank_lines(self):
        text = render(project(INDIVIDUAL_ENRICHED))
        assert "\n\n" not in text

    def test_org_renders_organization_name(self):
        text = render(project(ORGANIZATION_ENRICHED))
        assert "organization_name: MAYO CLINIC" in text

    def test_org_mailing_omitted_when_same(self):
        text = render(project(ORGANIZATION_ENRICHED))
        assert "mailing_address" not in text

    def test_geocoder_failed_renders_unresolved(self):
        text = render(project(INDIVIDUAL_GEOCODER_FAILED))
        assert "county_resolution_status: unresolved" in text
        assert "county_trust_tier: low" in text

    def test_foreign_renders_flag(self):
        text = render(project(INDIVIDUAL_FOREIGN))
        assert "foreign_provider: yes" in text
        assert "county_resolution_status: excluded" in text

    def test_inactive_renders_npi_status(self):
        text = render(project(INDIVIDUAL_INACTIVE))
        assert "npi_status: inactive" in text

    def test_field_order_stable_individual(self):
        text = render(project(INDIVIDUAL_ENRICHED))
        lines = text.splitlines()
        keys = [line.split(":")[0] for line in lines]
        record_type_idx   = keys.index("record_type")
        entity_type_idx   = keys.index("entity_type")
        display_name_idx  = keys.index("display_name")
        npi_idx           = keys.index("npi")
        assert record_type_idx < entity_type_idx < display_name_idx < npi_idx

    def test_system_fields_not_in_output(self):
        text = render(project(INDIVIDUAL_ENRICHED))
        # Check as label keys (with colon), not bare substrings — _id is a substring of other_identifiers
        assert "load_id:" not in text
        assert "record_id:" not in text
        assert "worker_id:" not in text
        assert "_id:" not in text


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
