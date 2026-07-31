# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for provider_flags_engine: no-fallback discipline + level-1
credential parse for can_prescribe.

Every test exercises pure functions (no Mongo). The engine's public
entry point apply_provider_flags is exercised via a fake Mongo double so
we prove the load + iterate + write shape without a live cluster.
"""

from __future__ import annotations

import pytest

from chathealthy_frontend_lib.exceptions import ChatHealthyException

from provider_flags_engine import (
    _credential_grants_prescribing,
    _primary_taxonomy_code,
    _apply_flags_to_doc,
    _load_catalog,
    apply_provider_flags,
)


# ---------- _credential_grants_prescribing ----------

@pytest.mark.unit
@pytest.mark.parametrize("cred", [
    "MD", "M.D.", "MD, PhD", "MD, FACS", "md",
    "DO", "D.O.", "DO, MPH", "do",
    "MD, DO",  # both tokens present
    "PhD, MD",  # order doesn't matter
])
def test_credential_grants_prescribing_yes(cred):
    assert _credential_grants_prescribing(cred) is True


@pytest.mark.unit
@pytest.mark.parametrize("cred", [
    None, "", "PhD", "RN", "PA-C", "NP", "DMD",
    "MDA",  # substring but not a token match
    "DOM",  # not a token match
    "PharmD",
])
def test_credential_grants_prescribing_no(cred):
    assert _credential_grants_prescribing(cred) is False


# ---------- _primary_taxonomy_code ----------

@pytest.mark.unit
def test_primary_taxonomy_code_prefers_explicit_primary():
    # New shape: primary_taxonomy_code lives top-level; taxonomies[]
    # entries are just {code, code_label} with no per-entry primary flag.
    doc = {"npi": "1", "primary_taxonomy_code": "BBB", "taxonomies": [
        {"code": "AAA"}, {"code": "BBB"}, {"code": "CCC"},
    ]}
    assert _primary_taxonomy_code(doc) == "BBB"


@pytest.mark.unit
def test_primary_taxonomy_code_raises_when_missing():
    """No fallback to taxonomies[0] -- missing primary_taxonomy_code
    means either NPPES did not designate a primary (source-data issue)
    or normalize failed to lift it. Both must surface."""
    with pytest.raises(ChatHealthyException) as exc_info:
        _primary_taxonomy_code({"npi": "1", "taxonomies": [{"code": "AAA"}]})
    assert exc_info.value.mode == "provider_missing_primary_taxonomy_code"


@pytest.mark.unit
def test_primary_taxonomy_code_raises_when_empty_string():
    with pytest.raises(ChatHealthyException) as exc_info:
        _primary_taxonomy_code({"npi": "1", "primary_taxonomy_code": ""})
    assert exc_info.value.mode == "provider_missing_primary_taxonomy_code"


# ---------- _apply_flags_to_doc ----------

_CATALOG = {
    "207Y00000X": {"can_prescribe": True, "is_homeopathic": False},
    "175F00000X": {"can_prescribe": False, "is_homeopathic": True},
    "133N00000X": {"can_prescribe": False, "is_homeopathic": False},
}


@pytest.mark.unit
def test_apply_flags_md_credential_overrides_catalog_false():
    """Level 1: MD credential grants can_prescribe even when the catalog
    row for the taxonomy says can_prescribe=False."""
    doc = {"npi": "1", "provider_credential_text": "MD",
           "primary_taxonomy_code": "133N00000X", "taxonomies": [{"code": "133N00000X"}]}
    flags = _apply_flags_to_doc(doc, catalog=_CATALOG, registered_npis=set())
    assert flags["can_prescribe"] is True
    assert flags["is_homeopathic"] is False


@pytest.mark.unit
def test_apply_flags_catalog_true_used_when_credential_absent():
    """Level 2 fallback: no MD/DO credential -> read catalog."""
    doc = {"npi": "1", "provider_credential_text": None,
           "primary_taxonomy_code": "207Y00000X", "taxonomies": [{"code": "207Y00000X"}]}
    flags = _apply_flags_to_doc(doc, catalog=_CATALOG, registered_npis=set())
    assert flags["can_prescribe"] is True


@pytest.mark.unit
def test_apply_flags_catalog_false_yields_false():
    """Non-MD credential + catalog can_prescribe=False -> False."""
    doc = {"npi": "1", "provider_credential_text": "PhD",
           "primary_taxonomy_code": "133N00000X", "taxonomies": [{"code": "133N00000X"}]}
    flags = _apply_flags_to_doc(doc, catalog=_CATALOG, registered_npis=set())
    assert flags["can_prescribe"] is False


@pytest.mark.unit
def test_apply_flags_homeopathic_from_catalog():
    doc = {"npi": "1", "provider_credential_text": None,
           "primary_taxonomy_code": "175F00000X", "taxonomies": [{"code": "175F00000X"}]}
    flags = _apply_flags_to_doc(doc, catalog=_CATALOG, registered_npis=set())
    assert flags["is_homeopathic"] is True


@pytest.mark.unit
def test_apply_flags_raises_when_code_not_in_catalog():
    doc = {"npi": "1", "provider_credential_text": "MD",
           "primary_taxonomy_code": "ZZZZ99999X", "taxonomies": [{"code": "ZZZZ99999X"}]}
    with pytest.raises(ChatHealthyException) as exc_info:
        _apply_flags_to_doc(doc, catalog=_CATALOG, registered_npis=set())
    assert exc_info.value.mode == "taxonomy_code_missing_from_catalog"


@pytest.mark.unit
def test_apply_flags_npi_registered_true_when_in_set():
    doc = {"npi": "1234567890", "provider_credential_text": "MD",
           "primary_taxonomy_code": "207Y00000X", "taxonomies": [{"code": "207Y00000X"}]}
    flags = _apply_flags_to_doc(
        doc, catalog=_CATALOG, registered_npis={"1234567890"},
    )
    assert flags["is_npi_registered"] is True


@pytest.mark.unit
def test_apply_flags_npi_registered_false_when_not_in_set():
    doc = {"npi": "1234567890", "provider_credential_text": "MD",
           "primary_taxonomy_code": "207Y00000X", "taxonomies": [{"code": "207Y00000X"}]}
    flags = _apply_flags_to_doc(
        doc, catalog=_CATALOG, registered_npis={"9999999999"},
    )
    assert flags["is_npi_registered"] is False


# ---------- _load_catalog ----------

class _FakeCollection:
    def __init__(self, rows):
        self._rows = rows

    def find(self, filt):
        return iter(self._rows)


class _FakeDB(dict):
    def __getitem__(self, name):
        return dict.__getitem__(self, name)


class _FakeMongo:
    def __init__(self, coll_rows):
        self._db = _FakeDB()
        # staging_collection_name("specialty_catalog", 3) -> "StagingSpecialtyCatalog_v_3"
        self._db["StagingSpecialtyCatalog_v_3"] = _FakeCollection(coll_rows)
        # nppes staging (not exercised in _load_catalog tests)
        self._db["StagingProvider_v_3"] = _FakeCollection([])

    def __getitem__(self, db_name):
        return self._db


@pytest.mark.unit
def test_load_catalog_happy_path():
    mongo = _FakeMongo([
        {"_id": 1, "raw": {"Code": "207Y00000X",
                            "can_prescribe": True, "homeopathic": False}},
        {"_id": 2, "raw": {"Code": "175F00000X",
                            "can_prescribe": False, "homeopathic": True}},
    ])
    catalog = _load_catalog(mongo, data_version=3)
    assert catalog["207Y00000X"] == {"can_prescribe": True, "is_homeopathic": False}
    assert catalog["175F00000X"] == {"can_prescribe": False, "is_homeopathic": True}


@pytest.mark.unit
def test_load_catalog_raises_on_empty_collection():
    mongo = _FakeMongo([])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, data_version=3)
    assert exc_info.value.mode == "catalog_empty"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_raw():
    mongo = _FakeMongo([{"_id": 1, "notraw": {}}])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, data_version=3)
    assert exc_info.value.mode == "catalog_row_malformed"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_code():
    mongo = _FakeMongo([{"_id": 1, "raw": {"can_prescribe": True, "homeopathic": False}}])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, data_version=3)
    assert exc_info.value.mode == "catalog_row_missing_code"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_can_prescribe():
    mongo = _FakeMongo([{"_id": 1, "raw": {"Code": "AAA", "homeopathic": False}}])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, data_version=3)
    assert exc_info.value.mode == "catalog_row_missing_can_prescribe"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_homeopathic():
    mongo = _FakeMongo([{"_id": 1, "raw": {"Code": "AAA", "can_prescribe": True}}])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, data_version=3)
    assert exc_info.value.mode == "catalog_row_missing_homeopathic"
