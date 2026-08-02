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


class _FakeFrontendMongo:
    """provider_flags_engine._load_catalog(frontend_client, env_prefix)
    reads from {env_prefix}_PublicHealthData.SpecialtyMetaData on the
    front-end cluster. This fake wires that path for the tests."""
    def __init__(self, coll_rows, env_prefix="dev"):
        self._db_name = f"{env_prefix}_PublicHealthData"
        self._db = _FakeDB()
        self._db["SpecialtyMetaData"] = _FakeCollection(coll_rows)

    def __getitem__(self, db_name):
        assert db_name == self._db_name, (
            f"unexpected db lookup: {db_name!r} (expected {self._db_name!r})"
        )
        return self._db


@pytest.mark.unit
def test_load_catalog_happy_path():
    # normalize_nucc publishes flattened top-level fields (Code, can_prescribe,
    # is_homeopathic, is_supplemented) into SpecialtyMetaData; _load_catalog
    # reads them directly from top level, not from a raw sub-doc.
    mongo = _FakeFrontendMongo([
        {"_id": 1, "Code": "207Y00000X",
         "can_prescribe": True, "is_homeopathic": False, "is_supplemented": False},
        {"_id": 2, "Code": "175F00000X",
         "can_prescribe": False, "is_homeopathic": True, "is_supplemented": False},
    ])
    catalog = _load_catalog(mongo, env_prefix="dev")
    assert catalog["207Y00000X"] == {
        "can_prescribe": True, "is_homeopathic": False, "is_supplemented": False,
    }
    assert catalog["175F00000X"] == {
        "can_prescribe": False, "is_homeopathic": True, "is_supplemented": False,
    }


@pytest.mark.unit
def test_load_catalog_raises_on_empty_collection():
    mongo = _FakeFrontendMongo([])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, env_prefix="dev")
    assert exc_info.value.mode == "catalog_empty"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_code():
    mongo = _FakeFrontendMongo([
        {"_id": 1, "can_prescribe": True, "is_homeopathic": False},
    ])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, env_prefix="dev")
    assert exc_info.value.mode == "catalog_row_missing_code"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_can_prescribe():
    mongo = _FakeFrontendMongo([
        {"_id": 1, "Code": "AAA", "is_homeopathic": False},
    ])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, env_prefix="dev")
    assert exc_info.value.mode == "catalog_row_missing_can_prescribe"


@pytest.mark.unit
def test_load_catalog_raises_on_missing_homeopathic():
    mongo = _FakeFrontendMongo([
        {"_id": 1, "Code": "AAA", "can_prescribe": True},
    ])
    with pytest.raises(ChatHealthyException) as exc_info:
        _load_catalog(mongo, env_prefix="dev")
    assert exc_info.value.mode == "catalog_row_missing_homeopathic"
