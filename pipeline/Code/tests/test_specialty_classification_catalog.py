"""Unit tests for the proprietary NUCC classification catalog loader.

Requires AZURE_STORAGE_CONNECTION_STRING in env so the loader can read
findcarestorage / chathealthy-public-data /
enrichment_content/specialty_classification_gpt41.json.
"""
import os
import sys
from pathlib import Path

import pytest

# Allow tests to find the pipeline modules sibling to this dir.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Load Code/.env for AZURE_STORAGE_CONNECTION_STRING when run locally.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_ENV = _REPO_ROOT / "Code" / ".env"
if _ENV.is_file():
    for _line in _ENV.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))


@pytest.fixture(autouse=True)
def _clear_catalog_cache():
    # Reset the in-process cache between tests.
    from specialty_classification_catalog import clear_cache
    clear_cache()
    yield
    clear_cache()


def test_catalog_loads_from_blob():
    from specialty_classification_catalog import load_catalog
    cat = load_catalog()
    assert isinstance(cat, dict)
    assert len(cat) >= 800, f"expected >=800 NUCC codes, got {len(cat)}"


def test_catalog_entry_shape():
    from specialty_classification_catalog import load_catalog
    cat = load_catalog()
    for code, entry in cat.items():
        assert isinstance(entry, dict)
        assert set(entry.keys()) == {"can_prescribe", "is_homeopathic"}
        assert isinstance(entry["can_prescribe"], bool)
        assert isinstance(entry["is_homeopathic"], bool)


def test_catalog_has_known_md_codes():
    # Allopathic & Osteopathic Physicians — taxonomy codes that start with
    # 207 should overwhelmingly be can_prescribe=True. Sample a few well-known
    # codes (Family Medicine 207Q00000X, Internal Medicine 207R00000X).
    from specialty_classification_catalog import load_catalog
    cat = load_catalog()
    for code in ("207Q00000X", "207R00000X"):
        assert code in cat, f"expected {code} in catalog"
        assert cat[code]["can_prescribe"] is True, f"{code} should be a prescriber"


def test_apply_proprietary_flags_against_synthetic_providers(monkeypatch):
    # Build a fake collection that captures bulk_write ops so we can assert
    # the flags landed without touching real Mongo.
    from county_enrichment_job import apply_proprietary_flags_fn

    docs = [
        {"_id": "a", "npi": "1111111111", "entity_type_code": "1",
         "taxonomies": [{"code": "207Q00000X", "primary": True}]},  # MD - prescriber
        {"_id": "b", "npi": "2222222222", "entity_type_code": "2",
         "taxonomies": [{"code": "207Q00000X", "primary": True}]},  # Org -> false
        {"_id": "c", "npi": "3333333333", "entity_type_code": "1",
         "taxonomies": [{"code": "175L00000X", "primary": True}]},  # homeopath
    ]
    writes = []

    class FakeBulkResult:
        def __init__(self, n):
            self.modified_count = n
            self.matched_count = n

    class FakeColl:
        def count_documents(self, q):
            return len(docs)

        def find(self, q, proj=None):
            return iter(docs)

        def bulk_write(self, ops, ordered=False):
            writes.extend(ops)
            return FakeBulkResult(len(ops))

    class FakeDb:
        def __getitem__(self, name):
            return FakeColl()

    class FakeClient:
        def __getitem__(self, name):
            return FakeDb()

    monkeypatch.setattr("county_enrichment_job._get_mongo_client", lambda: FakeClient())

    res = apply_proprietary_flags_fn({
        "provider_collection": "x.y",
        "states": ["NE"],
    })
    assert res["classified"] == 3
    # Verify each provider got the right combination
    by_id = {}
    for op in writes:
        # UpdateOne stores filter + update; we read them via the public attrs
        flt = op._filter
        upd = op._doc
        sets = upd.get("$set", {})
        by_id[flt["_id"]] = sets
    assert by_id["a"] == {"can_prescribe": True, "is_homeopathic": False}
    assert by_id["b"] == {"can_prescribe": False, "is_homeopathic": False}
    assert by_id["c"] == {"can_prescribe": False, "is_homeopathic": True}
