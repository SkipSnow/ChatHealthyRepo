# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pytest: Specialty classification flags on SpecialtyMetaData
# Tests can_prescribe, homeopathic, and future pediatric_applicability flags.
# Runs against live MongoDB (GOV-006: real system testing).
#
# Requirements tested:
#   EPIC-006-F-010-S-004-REQ-T-001 through REQ-005: can_prescribe flag
#   FINDCARE-UX-003: prescriber filter checkbox
#   FINDCARE-UX-007: client-side cached filter list
#   FINDCARE-PED-001: pediatric_applicability field (future)
#   RISK-002: AI classification acceptance
#
# Requires: MONGO_READONLY_FRONTEND or MONGO_FRONTEND_connectionString env var
# Run: pytest Code/DataPipelines/tests/test_specialty_classification.py -v -s

import os
import sys
import pytest

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


# Rule-004: one place in this file obtains a connection, and it goes through
# the canonical utility. The certificate is the credential; there is no
# connection string here and no fallback. Raises if the identity cannot
# connect, which is the point -- a test that quietly connects as something
# else proves nothing about production.
def _ch_connection():
    import sys as _sys, pathlib as _pl
    for _d in _pl.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _lib = _d / "FrontEndApplicationLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", 'frontEnd')

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_ENV = os.environ.get("ENV_PREFIX", "dev")
_DB_NAME = f"{_ENV}_PublicHealthData"


def _get_connection_string():
    for key in ("MONGO_READONLY_FRONTEND", "MONGO_FRONTEND_connectionString"):
        val = os.environ.get(key)
        if val:
            return val
    pytest.skip("No frontend MongoDB connection string in env")


@pytest.fixture(scope="module")
def specialty_coll():
    from pymongo import MongoClient
    client = _ch_connection()
    return client[_DB_NAME]["SpecialtyMetaData"]


@pytest.fixture(scope="module")
def all_specialties(specialty_coll):
    return list(specialty_coll.find({}, {"_id": 0, "Code": 1, "Display Name": 1,
                                         "can_prescribe": 1, "homeopathic": 1,
                                         "pediatric_applicability": 1}))


# ===========================================================================
# 1. SpecialtyMetaData completeness
# ===========================================================================

class TestSpecialtyCompleteness:
    """SpecialtyMetaData must have all NUCC codes with classification flags."""

    def test_has_specialties(self, specialty_coll):
        """Must have 800+ NUCC specialty codes."""
        count = specialty_coll.count_documents({})
        assert count >= 800, f"Only {count} specialties, expected 800+"
        _CH_LOG.info(f"\n  Total specialties: {count}")

    def test_all_have_can_prescribe(self, all_specialties):
        """Every specialty must have can_prescribe flag (EPIC-006-F-010-S-004-REQ-T-001)."""
        missing = [s for s in all_specialties if "can_prescribe" not in s]
        assert len(missing) == 0, (
            f"{len(missing)} specialties missing can_prescribe: "
            f"{[s.get('Code') for s in missing[:5]]}"
        )

    def test_all_have_homeopathic(self, all_specialties):
        """Every specialty must have homeopathic flag."""
        missing = [s for s in all_specialties if "homeopathic" not in s]
        assert len(missing) == 0, (
            f"{len(missing)} specialties missing homeopathic: "
            f"{[s.get('Code') for s in missing[:5]]}"
        )

    def test_can_prescribe_is_boolean(self, all_specialties):
        """can_prescribe must be boolean, not string."""
        non_bool = [s for s in all_specialties
                    if "can_prescribe" in s and not isinstance(s["can_prescribe"], bool)]
        assert len(non_bool) == 0, (
            f"{len(non_bool)} specialties have non-boolean can_prescribe"
        )

    def test_homeopathic_is_boolean(self, all_specialties):
        """homeopathic must be boolean, not string."""
        non_bool = [s for s in all_specialties
                    if "homeopathic" in s and not isinstance(s["homeopathic"], bool)]
        assert len(non_bool) == 0, (
            f"{len(non_bool)} specialties have non-boolean homeopathic"
        )


# ===========================================================================
# 2. Prescriber classification sanity checks
# ===========================================================================

class TestPrescriberClassification:
    """Known specialties must be correctly classified (RISK-002 audit)."""

    def test_prescriber_count_reasonable(self, all_specialties):
        """Between 200-500 specialties should be prescribers."""
        prescribers = [s for s in all_specialties if s.get("can_prescribe")]
        count = len(prescribers)
        assert 200 <= count <= 500, (
            f"Prescriber count {count} outside expected range 200-500"
        )
        _CH_LOG.info(f"\n  Prescribers: {count}")

    def test_homeopathic_count_reasonable(self, all_specialties):
        """Between 5-50 specialties should be homeopathic."""
        homeo = [s for s in all_specialties if s.get("homeopathic")]
        count = len(homeo)
        assert 5 <= count <= 50, (
            f"Homeopathic count {count} outside expected range 5-50"
        )
        _CH_LOG.info(f"\n  Homeopathic: {count}")

    def test_physicians_can_prescribe(self, specialty_coll):
        """All physician specialties (207*, 208*) must be can_prescribe=True."""
        physicians = list(specialty_coll.find(
            {"Code": {"$regex": "^(207|208)"}, "can_prescribe": {"$ne": True}},
            {"Code": 1, "Display Name": 1, "_id": 0},
        ))
        assert len(physicians) == 0, (
            f"{len(physicians)} physician specialties NOT marked can_prescribe: "
            f"{[p.get('Code') for p in physicians[:5]]}"
        )

    def test_nurse_practitioners_can_prescribe(self, specialty_coll):
        """Nurse Practitioner specialties (363L*) must be can_prescribe=True."""
        nps = list(specialty_coll.find(
            {"Code": {"$regex": "^363L"}, "can_prescribe": {"$ne": True}},
            {"Code": 1, "Display Name": 1, "_id": 0},
        ))
        assert len(nps) == 0, (
            f"{len(nps)} NP specialties NOT marked can_prescribe: "
            f"{[p.get('Code') for p in nps[:5]]}"
        )

    def test_physical_therapists_cannot_prescribe(self, specialty_coll):
        """Physical Therapist specialties (225*) must be can_prescribe=False."""
        pts = list(specialty_coll.find(
            {"Code": {"$regex": "^225"}, "can_prescribe": True},
            {"Code": 1, "Display Name": 1, "_id": 0},
        ))
        assert len(pts) == 0, (
            f"{len(pts)} PT specialties incorrectly marked can_prescribe: "
            f"{[p.get('Code') for p in pts[:5]]}"
        )

    def test_social_workers_cannot_prescribe(self, specialty_coll):
        """Social Worker specialties (104*) must be can_prescribe=False."""
        sws = list(specialty_coll.find(
            {"Code": {"$regex": "^104"}, "can_prescribe": True},
            {"Code": 1, "Display Name": 1, "_id": 0},
        ))
        assert len(sws) == 0, (
            f"{len(sws)} social worker specialties incorrectly marked can_prescribe"
        )

    def test_naturopath_is_homeopathic(self, specialty_coll):
        """Naturopath (175F00000X) must be homeopathic=True."""
        doc = specialty_coll.find_one({"Code": "175F00000X"}, {"homeopathic": 1, "_id": 0})
        if doc:
            assert doc.get("homeopathic") is True, "Naturopath not marked homeopathic"

    def test_chiropractor_is_homeopathic(self, specialty_coll):
        """Chiropractor (111N*) must be homeopathic=True."""
        chiro = specialty_coll.find_one({"Code": {"$regex": "^111N"}}, {"homeopathic": 1, "_id": 0})
        if chiro:
            assert chiro.get("homeopathic") is True, "Chiropractor not marked homeopathic"


# ===========================================================================
# 3. Filter coexistence (FINDCARE-PED-007)
# ===========================================================================

class TestFilterCoexistence:
    """Multiple filters must not conflict."""

    def test_prescriber_and_homeopathic_overlap(self, all_specialties):
        """Some specialties can be both prescriber AND homeopathic (e.g., naturopath in some states)."""
        both = [s for s in all_specialties if s.get("can_prescribe") and s.get("homeopathic")]
        # It's valid to have 0 overlap or some overlap — just report
        _CH_LOG.info(f"\n  Both prescriber + homeopathic: {len(both)}")
        for s in both[:5]:
            _CH_LOG.info(f"    {s.get('Code')}: {s.get('Display Name')}")

    def test_neither_prescriber_nor_homeopathic(self, all_specialties):
        """Some specialties are neither (e.g., PT, social worker)."""
        neither = [s for s in all_specialties
                   if not s.get("can_prescribe") and not s.get("homeopathic")]
        assert len(neither) > 100, (
            f"Only {len(neither)} specialties are neither prescriber nor homeopathic — expected 100+"
        )
        _CH_LOG.info(f"\n  Neither prescriber nor homeopathic: {len(neither)}")


# ===========================================================================
# 4. Crosswalk integration (provider-level)
# ===========================================================================

class TestCrosswalkOnProviders:
    """Verify crosswalk flags on providers match specialty classifications."""

    @pytest.fixture(scope="class")
    def providers_coll(self):
        from pymongo import MongoClient
        conn = os.environ.get("MONGO_READONLY_FRONTEND",
                              os.environ.get("MONGO_FRONTEND_connectionString"))
        if not conn:
            pytest.skip("No frontend connection")
        client = _ch_connection()
        return client[_DB_NAME]["providers"]

    def test_providers_have_can_prescribe(self, providers_coll):
        """DE providers should have can_prescribe flag."""
        has = providers_coll.count_documents({"addresses.state": "DE", "can_prescribe": {"$exists": True}})
        total = providers_coll.count_documents({"addresses.state": "DE"})
        if total == 0:
            pytest.skip("No DE providers on frontend")
        rate = has / total
        _CH_LOG.info(f"\n  DE providers with can_prescribe: {has}/{total} ({rate:.0%})")
        assert rate > 0.90, f"Only {rate:.0%} of DE providers have can_prescribe flag"
