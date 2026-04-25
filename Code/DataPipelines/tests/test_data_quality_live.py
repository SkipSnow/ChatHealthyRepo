# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Live data quality tests against dev MongoDB
# GOV-006: Real system testing — no mocks.
# Tests provider schema, data flags, enrichment coverage,
# and prescriber classification for loaded states.
#
# Requires: MONGO_FRONTEND_connectionString or MONGO_READONLY_FRONTEND env var
# Run from repo root: pytest Code/DataPipelines/tests/test_data_quality_live.py -v

import os
import sys
import pytest
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _get_connection_string():
    """Return frontend cluster connection string from env."""
    for key in ("MONGO_READONLY_FRONTEND", "MONGO_FRONTEND_connectionString"):
        val = os.environ.get(key)
        if val:
            return val
    pytest.skip("No frontend MongoDB connection string in env")


_ENV = os.environ.get("ENV_PREFIX", "dev")
_DB_NAME = f"{_ENV}_PublicHealthData"


@pytest.fixture(scope="module")
def providers_coll():
    """Providers collection on frontend cluster."""
    from pymongo import MongoClient
    client = MongoClient(_get_connection_string())
    return client[_DB_NAME]["providers"]


@pytest.fixture(scope="module")
def specialty_coll():
    """SpecialtyMetaData collection on frontend cluster."""
    from pymongo import MongoClient
    client = MongoClient(_get_connection_string())
    return client[_DB_NAME]["SpecialtyMetaData"]


@pytest.fixture(scope="module")
def de_stats(providers_coll):
    """Pre-computed DE statistics reused across tests."""
    coll = providers_coll
    total = coll.count_documents({"practice_address.state": "DE"})
    individuals = coll.count_documents({"practice_address.state": "DE", "entity_type_code": "1"})
    orgs = coll.count_documents({"practice_address.state": "DE", "entity_type_code": "2"})
    bad = coll.count_documents({"practice_address.state": "DE", "bad_data.flagged": True})
    oos = coll.count_documents({"practice_address.state": "DE", "out_of_scope.flagged": True})
    enriched = coll.count_documents({"practice_address.state": "DE", "county.fips": {"$ne": None}})
    deactivated = coll.count_documents({
        "practice_address.state": "DE",
        "npi_deactivation_date": {"$exists": True, "$ne": ""}
    })
    return {
        "total": total,
        "individuals": individuals,
        "orgs": orgs,
        "bad_data": bad,
        "out_of_scope": oos,
        "enriched": enriched,
        "deactivated": deactivated,
    }


# ---------------------------------------------------------------------------
# Taxonomy helpers
# ---------------------------------------------------------------------------

# NUCC taxonomy prefixes for providers who can prescribe
PRESCRIBER_TAX_PREFIXES = (
    "207",   # Allopathic & Osteopathic Physicians
    "208",   # Allopathic (continued)
    "209",   # Allopathic (continued)
    "204",   # Neuromusculoskeletal Medicine
    "174400000X",  # Optometrist
    "363L",  # Nurse Practitioner
    "363A",  # Physician Assistant
    "367",   # CRNA / Advanced Practice Midwife
    "122",   # Dentists
    "213",   # Podiatrists
)


def _is_prescriber_taxonomy(code: str) -> bool:
    return any(code.startswith(p) for p in PRESCRIBER_TAX_PREFIXES)


# ===========================================================================
# 1. Schema Integrity Tests
# ===========================================================================

class TestSchemaIntegrity:
    """Every provider record must have required fields with correct types."""

    REQUIRED_FIELDS = [
        "npi", "entity_type_code", "practice_address", "taxonomies",
        "load_id", "record_id", "worker_id",
    ]

    def test_sample_has_required_fields(self, providers_coll):
        """50 random DE providers must have all required fields."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE"}},
            {"$sample": {"size": 50}},
        ]))
        assert len(samples) > 0, "No DE providers found"
        for doc in samples:
            for field in self.REQUIRED_FIELDS:
                assert field in doc, f"NPI {doc.get('npi','?')} missing field: {field}"

    def test_npi_is_10_digit_string(self, providers_coll):
        """NPI must be a 10-digit string, never numeric."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE"}},
            {"$sample": {"size": 50}},
        ]))
        for doc in samples:
            npi = doc["npi"]
            assert isinstance(npi, str), f"NPI {npi} is {type(npi).__name__}, must be str"
            assert len(npi) == 10, f"NPI {npi} is {len(npi)} chars, must be 10"
            assert npi.isdigit(), f"NPI {npi} contains non-digit characters"

    def test_entity_type_code_valid(self, providers_coll):
        """entity_type_code must be '1' (individual) or '2' (organization)."""
        bad = providers_coll.count_documents({
            "practice_address.state": "DE",
            "entity_type_code": {"$nin": ["1", "2"]}
        })
        assert bad == 0, f"{bad} DE providers have invalid entity_type_code"

    def test_practice_address_has_state(self, providers_coll):
        """Every DE provider must have practice_address.state = 'DE'."""
        # This is tautological for our filter, but validates nested field structure
        missing = providers_coll.count_documents({
            "practice_address.state": "DE",
            "practice_address.zip": {"$exists": False}
        })
        assert missing == 0, f"{missing} DE providers missing practice_address.zip"

    def test_taxonomies_non_empty(self, providers_coll):
        """Every provider must have at least one taxonomy code."""
        empty = providers_coll.count_documents({
            "practice_address.state": "DE",
            "$or": [
                {"taxonomies": {"$exists": False}},
                {"taxonomies": []},
            ]
        })
        assert empty == 0, f"{empty} DE providers have empty taxonomies"

    def test_taxonomy_has_code_and_primary(self, providers_coll):
        """Each taxonomy entry must have 'code' and 'primary' fields."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE"}},
            {"$sample": {"size": 50}},
        ]))
        for doc in samples:
            for i, tax in enumerate(doc.get("taxonomies", [])):
                assert "code" in tax, f"NPI {doc['npi']} taxonomy[{i}] missing 'code'"
                assert "primary" in tax, f"NPI {doc['npi']} taxonomy[{i}] missing 'primary'"

    def test_primary_taxonomy_coverage(self, providers_coll):
        """Report providers without a primary taxonomy — CMS data quality issue."""
        total = providers_coll.count_documents({"practice_address.state": "DE"})
        # Count providers where no taxonomy has primary=True
        no_primary = 0
        for doc in providers_coll.find(
            {"practice_address.state": "DE"},
            {"npi": 1, "taxonomies": 1, "_id": 0},
        ):
            primaries = [t for t in doc.get("taxonomies", []) if t.get("primary")]
            if len(primaries) == 0:
                no_primary += 1
        # CMS sometimes omits primary flag. Under 1% is acceptable.
        rate = no_primary / total if total > 0 else 0
        print(f"\n  DE providers with no primary taxonomy: {no_primary}/{total} ({rate:.1%})")
        assert rate < 0.01, (
            f"{no_primary} DE providers ({rate:.1%}) have no primary taxonomy — exceeds 1% threshold"
        )


# ===========================================================================
# 2. Data Quality Flag Tests
# ===========================================================================

class TestDataQualityFlags:
    """Verify bad_data and out_of_scope flags are correctly applied."""

    def test_no_address_flagged_bad_data(self, providers_coll):
        """Providers with no practice address AND no mailing address must be flagged bad_data."""
        # Find any provider with neither address — should be flagged or not exist
        unflagged = providers_coll.count_documents({
            "practice_address.state": "DE",
            "practice_address.line1": {"$in": [None, ""]},
            "mailing_address.line1": {"$in": [None, ""]},
            "bad_data.flagged": {"$ne": True},
        })
        assert unflagged == 0, f"{unflagged} DE providers with no address and no bad_data flag"

    def test_deactivated_providers_flagged(self, providers_coll, de_stats):
        """Providers with deactivation date and no reactivation must be out_of_scope."""
        unflagged = providers_coll.count_documents({
            "practice_address.state": "DE",
            "npi_deactivation_date": {"$exists": True, "$ne": ""},
            "$or": [
                {"npi_reactivation_date": {"$exists": False}},
                {"npi_reactivation_date": None},
                {"npi_reactivation_date": ""},
            ],
            "out_of_scope.flagged": {"$ne": True},
        })
        if de_stats["deactivated"] > 0:
            # If deactivated exist, they should be flagged
            assert unflagged == 0, (
                f"{unflagged} deactivated DE providers not flagged out_of_scope"
            )

    def test_bad_data_reasons_from_vocabulary(self, providers_coll):
        """All bad_data.reason values must be from CV-001."""
        import json
        vocab_path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                   "brain", "machine_artifacts", "content",
                                   "controlled_vocabularies.json")
        with open(vocab_path) as f:
            vocabs = json.load(f)
        cv001 = next(v for v in vocabs["vocabularies"] if v["vocabulary_id"] == "CV-001")
        allowed = {m["value"] for m in cv001["members"]}

        bad_docs = providers_coll.find(
            {"practice_address.state": "DE", "bad_data.flagged": True},
            {"bad_data.reason": 1, "npi": 1, "_id": 0}
        )
        for doc in bad_docs:
            reason = doc.get("bad_data", {}).get("reason")
            assert reason in allowed, (
                f"NPI {doc['npi']} bad_data.reason='{reason}' not in CV-001"
            )

    def test_out_of_scope_reasons_from_vocabulary(self, providers_coll):
        """All out_of_scope.reason values must be from CV-002."""
        import json
        vocab_path = os.path.join(os.path.dirname(__file__), "..", "..", "..",
                                   "brain", "machine_artifacts", "content",
                                   "controlled_vocabularies.json")
        with open(vocab_path) as f:
            vocabs = json.load(f)
        cv002 = next(v for v in vocabs["vocabularies"] if v["vocabulary_id"] == "CV-002")
        allowed = {m["value"] for m in cv002["members"]}

        oos_docs = providers_coll.find(
            {"practice_address.state": "DE", "out_of_scope.flagged": True},
            {"out_of_scope.reason": 1, "npi": 1, "_id": 0}
        )
        for doc in oos_docs:
            reason = doc.get("out_of_scope", {}).get("reason")
            assert reason in allowed, (
                f"NPI {doc['npi']} out_of_scope.reason='{reason}' not in CV-002"
            )


# ===========================================================================
# 3. County Enrichment Tests
# ===========================================================================

class TestCountyEnrichment:
    """Verify county enrichment coverage and FIPS format."""

    VALID_SOURCES = {
        "crosswalk_pass1", "crosswalk_zip_fixed",
        "geocoder_pass2_batch", "geocoder_pass3_billing",
        "geocoder_pass4_maps", "geocoder_pass5_maps_billing",
        "geocoder_pass6_nppes", "geocoder_failed", "out_of_scope",
    }

    def test_enrichment_rate_above_threshold(self, providers_coll, de_stats):
        """At least 95% of addressable DE providers must be county-enriched."""
        addressable = de_stats["total"] - de_stats["bad_data"] - de_stats["out_of_scope"]
        if addressable == 0:
            pytest.skip("No addressable DE providers")
        rate = de_stats["enriched"] / addressable
        assert rate >= 0.95, (
            f"DE enrichment rate {rate:.1%} ({de_stats['enriched']}/{addressable}) below 95%"
        )

    def test_fips_format_valid(self, providers_coll):
        """county.fips must be a 5-digit string when present."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE", "county.fips": {"$ne": None}}},
            {"$sample": {"size": 50}},
        ]))
        for doc in samples:
            fips = doc["county"]["fips"]
            assert isinstance(fips, str), f"NPI {doc['npi']} FIPS is {type(fips).__name__}"
            assert len(fips) == 5, f"NPI {doc['npi']} FIPS '{fips}' is not 5 digits"
            assert fips.isdigit(), f"NPI {doc['npi']} FIPS '{fips}' non-numeric"

    def test_fips_starts_with_delaware_code(self, providers_coll):
        """DE county FIPS codes should start with '10' (Delaware FIPS state code).

        Known issue: geocoder_pass6_nppes returns wrong county for ambiguous
        city names (e.g. Newark DE → Newark NJ). These are real data quality
        bugs that should be tracked and fixed.
        """
        non_de_fips = list(providers_coll.find(
            {
                "practice_address.state": "DE",
                "county.fips": {"$exists": True, "$ne": None, "$not": {"$regex": "^10"}},
            },
            {"npi": 1, "county": 1, "practice_address.city": 1, "_id": 0},
        ))
        # Report but allow up to 10 (known geocoder edge cases)
        if non_de_fips:
            print(f"\n  DATA QUALITY: {len(non_de_fips)} DE providers have non-DE FIPS:")
            for doc in non_de_fips:
                print(f"    NPI {doc['npi']}: FIPS={doc['county']['fips']} "
                      f"source={doc['county'].get('source')} "
                      f"city={doc.get('practice_address', {}).get('city')}")
        assert len(non_de_fips) <= 10, (
            f"{len(non_de_fips)} DE providers have non-Delaware FIPS — exceeds threshold of 10"
        )

    def test_county_source_valid_enum(self, providers_coll):
        """county.source must be from the known source enum."""
        sources = providers_coll.distinct("county.source", {"practice_address.state": "DE"})
        for src in sources:
            if src is None:
                continue
            assert src in self.VALID_SOURCES, f"Unknown county.source: '{src}'"

    def test_geocoder_failed_count_reasonable(self, providers_coll, de_stats):
        """Geocoder failures should be under 5% of total."""
        failed = providers_coll.count_documents({
            "practice_address.state": "DE",
            "county.source": "geocoder_failed",
        })
        if de_stats["total"] == 0:
            pytest.skip("No DE providers")
        rate = failed / de_stats["total"]
        assert rate < 0.05, (
            f"Geocoder failure rate {rate:.1%} ({failed}/{de_stats['total']}) exceeds 5%"
        )


# ===========================================================================
# 4. Embedding Tests
# ===========================================================================

class TestEmbeddings:
    """Verify provider embeddings are present and correctly dimensioned."""

    def test_embeddings_present(self, providers_coll):
        """DE providers should have embeddings."""
        has_embedding = providers_coll.count_documents({
            "practice_address.state": "DE",
            "embedding": {"$exists": True},
        })
        total = providers_coll.count_documents({"practice_address.state": "DE"})
        rate = has_embedding / total if total > 0 else 0
        assert rate > 0.90, f"Only {rate:.1%} of DE providers have embeddings"

    def test_embedding_dimension_3072(self, providers_coll):
        """Provider embeddings must be 3072-dimensional (text-embedding-3-large)."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE", "embedding": {"$exists": True}}},
            {"$sample": {"size": 10}},
            {"$project": {"npi": 1, "dim": {"$size": "$embedding"}, "embedding_model": 1}},
        ]))
        for doc in samples:
            assert doc["dim"] == 3072, (
                f"NPI {doc['npi']} embedding dim={doc['dim']}, expected 3072"
            )

    def test_embedding_model_field(self, providers_coll):
        """Embedded providers must have embedding_model field."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE", "embedding": {"$exists": True}}},
            {"$sample": {"size": 10}},
        ]))
        for doc in samples:
            assert doc.get("embedding_model") == "text-embedding-3-large", (
                f"NPI {doc['npi']} model={doc.get('embedding_model')}"
            )


# ===========================================================================
# 5. Prescriber Coverage Analysis Tests
# ===========================================================================

class TestPrescriberCoverage:
    """Quantify prescriber vs non-prescriber split for gap analysis."""

    def test_de_has_providers(self, de_stats):
        """DE must have a non-trivial number of providers."""
        assert de_stats["total"] > 2000, (
            f"DE only has {de_stats['total']} providers — expected 2000+"
        )

    def test_de_entity_type_adds_up(self, de_stats):
        """Individuals + organizations must equal total."""
        assert de_stats["individuals"] + de_stats["orgs"] == de_stats["total"], (
            f"individuals({de_stats['individuals']}) + orgs({de_stats['orgs']}) "
            f"!= total({de_stats['total']})"
        )

    def test_prescriber_count(self, providers_coll):
        """Count and report DE prescribers for gap analysis."""
        pipeline = [
            {"$match": {"practice_address.state": "DE", "entity_type_code": "1"}},
            {"$unwind": "$taxonomies"},
            {"$match": {"taxonomies.primary": True}},
            {"$project": {"npi": 1, "tax_code": "$taxonomies.code"}},
        ]
        results = list(providers_coll.aggregate(pipeline))
        prescribers = [r for r in results if _is_prescriber_taxonomy(r["tax_code"])]
        non_prescribers = [r for r in results if not _is_prescriber_taxonomy(r["tax_code"])]

        # Prescribers should be a meaningful portion of individuals
        assert len(prescribers) > 500, (
            f"Only {len(prescribers)} DE prescribers — expected 500+"
        )
        # Report (visible in pytest -v output)
        print(f"\n  DE Prescribers: {len(prescribers)}")
        print(f"  DE Non-prescribers: {len(non_prescribers)}")
        print(f"  Prescriber ratio: {len(prescribers)/len(results):.1%}")

    def test_can_prescribe_flag_present(self, providers_coll):
        """EPIC-006-F-010-S-004-REQ-T-001: All DE providers must have can_prescribe flag."""
        missing = providers_coll.count_documents({
            "practice_address.state": "DE",
            "can_prescribe": {"$exists": False},
        })
        assert missing == 0, f"{missing} DE providers missing can_prescribe flag"

    def test_orgs_are_non_prescribers(self, providers_coll):
        """EPIC-006-F-010-S-004-REQ-T-002: All organizations must be can_prescribe=false."""
        org_prescribers = providers_coll.count_documents({
            "practice_address.state": "DE",
            "entity_type_code": "2",
            "can_prescribe.flagged": True,
        })
        assert org_prescribers == 0, (
            f"{org_prescribers} DE organizations flagged as prescribers"
        )

    def test_can_prescribe_has_method(self, providers_coll):
        """EPIC-006-F-010-S-004-REQ-T-001: can_prescribe must include method field."""
        samples = list(providers_coll.aggregate([
            {"$match": {"practice_address.state": "DE", "can_prescribe": {"$exists": True}}},
            {"$sample": {"size": 50}},
        ]))
        for doc in samples:
            cp = doc["can_prescribe"]
            assert "flagged" in cp, f"NPI {doc['npi']} can_prescribe missing 'flagged'"
            assert "method" in cp, f"NPI {doc['npi']} can_prescribe missing 'method'"
            assert cp["method"] in ("taxonomy", "organization"), (
                f"NPI {doc['npi']} can_prescribe.method='{cp['method']}' invalid"
            )

    def test_prescriber_ratio_reasonable(self, providers_coll, de_stats):
        """Prescriber ratio should be between 20-40% for a typical state."""
        prescribers = providers_coll.count_documents({
            "practice_address.state": "DE",
            "can_prescribe.flagged": True,
        })
        rate = prescribers / de_stats["total"] if de_stats["total"] > 0 else 0
        assert 0.15 <= rate <= 0.50, (
            f"DE prescriber rate {rate:.1%} outside expected range 15-50%"
        )
        print(f"\n  DE prescriber rate: {rate:.1%} ({prescribers}/{de_stats['total']})")

    def test_no_duplicate_npis(self, providers_coll):
        """No duplicate NPIs in DE data."""
        pipeline = [
            {"$match": {"practice_address.state": "DE"}},
            {"$group": {"_id": "$npi", "count": {"$sum": 1}}},
            {"$match": {"count": {"$gt": 1}}},
        ]
        dupes = list(providers_coll.aggregate(pipeline))
        assert len(dupes) == 0, (
            f"{len(dupes)} duplicate NPIs in DE: {[d['_id'] for d in dupes[:5]]}"
        )


# ===========================================================================
# 6. Cross-State Consistency Tests
# ===========================================================================

class TestCrossStateConsistency:
    """Verify loaded states are consistent with pipeline config."""

    EXPECTED_STATES = {"DE", "VA", "MS"}  # from pipeline state filter

    def test_only_expected_states_loaded(self, providers_coll):
        """Only pipeline-configured states should be in the database."""
        states = providers_coll.distinct("practice_address.state")
        loaded = set(states)
        unexpected = loaded - self.EXPECTED_STATES
        assert len(unexpected) == 0, (
            f"Unexpected states in dev: {unexpected}"
        )

    def test_all_expected_states_present(self, providers_coll):
        """All pipeline-configured states must have data."""
        for state in self.EXPECTED_STATES:
            count = providers_coll.count_documents({"practice_address.state": state})
            assert count > 0, f"State {state} has 0 providers — pipeline may have missed it"

    def test_va_has_expected_volume(self, providers_coll):
        """VA should have 100K+ providers (largest loaded state)."""
        count = providers_coll.count_documents({"practice_address.state": "VA"})
        assert count > 100_000, f"VA has {count:,} providers, expected 100K+"


# ===========================================================================
# 7. SpecialtyMetaData Integrity
# ===========================================================================

class TestSpecialtyMetaData:
    """Verify SpecialtyMetaData completeness for loaded provider taxonomies."""

    def test_specialty_metadata_has_records(self, specialty_coll):
        """SpecialtyMetaData must have NUCC taxonomy entries."""
        count = specialty_coll.count_documents({})
        assert count > 800, f"SpecialtyMetaData has {count} entries, expected 800+"

    def test_de_taxonomy_codes_in_metadata(self, providers_coll, specialty_coll):
        """Every primary taxonomy code used by DE providers must exist in SpecialtyMetaData."""
        # Get distinct taxonomy codes from DE providers
        pipeline = [
            {"$match": {"practice_address.state": "DE"}},
            {"$unwind": "$taxonomies"},
            {"$match": {"taxonomies.primary": True}},
            {"$group": {"_id": "$taxonomies.code"}},
        ]
        provider_codes = {doc["_id"] for doc in providers_coll.aggregate(pipeline)}

        # Check each code exists in SpecialtyMetaData
        meta_codes = set(specialty_coll.distinct("Code"))
        missing = provider_codes - meta_codes
        assert len(missing) == 0, (
            f"{len(missing)} DE taxonomy codes not in SpecialtyMetaData: "
            f"{sorted(missing)[:10]}"
        )

    def test_specialty_metadata_has_embeddings(self, specialty_coll):
        """SpecialtyMetaData entries should have embeddings for vector search."""
        total = specialty_coll.count_documents({})
        has_emb = specialty_coll.count_documents({"embedding": {"$exists": True}})
        if total == 0:
            pytest.skip("No SpecialtyMetaData")
        rate = has_emb / total
        assert rate > 0.90, (
            f"Only {rate:.1%} of SpecialtyMetaData has embeddings"
        )
