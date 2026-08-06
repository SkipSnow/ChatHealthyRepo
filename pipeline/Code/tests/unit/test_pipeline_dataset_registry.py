# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for pipeline_dataset_registry.PipelineDatasetRegistry."""

from __future__ import annotations

import copy

import pytest

from chathealthy_frontend_lib.exceptions import ChatHealthyException

from pipeline_dataset_registry import (
    DatasetEntry,
    PipelineDatasetRegistry,
)


# ---------------------------------------------------------------------------
# fake mongo (captures inserts into Pipelines.pipeline.discrepancies)
# ---------------------------------------------------------------------------


class _FakeColl:
    def __init__(self):
        self.docs: list[dict] = []

    def insert_one(self, doc: dict):
        self.docs.append(dict(doc))


class _FakeDb:
    def __init__(self):
        self.colls: dict[str, _FakeColl] = {}

    def __getitem__(self, name):
        return self.colls.setdefault(name, _FakeColl())


class _FakeMongo:
    def __init__(self):
        self.dbs: dict[str, _FakeDb] = {}

    def __getitem__(self, name):
        return self.dbs.setdefault(name, _FakeDb())

    @property
    def discrepancies(self):
        db = self.dbs.get("Pipelines")
        if db is None:
            return []
        coll = db.colls.get("pipeline.discrepancies")
        if coll is None:
            return []
        return coll.docs


@pytest.fixture
def fake_mongo():
    return _FakeMongo()


# ---------------------------------------------------------------------------
# baseline valid config
# ---------------------------------------------------------------------------


def _baseline_config() -> dict:
    return {
        "dataset_versions": [
            {
                "source_name": "nppes_npi",
                "bundled_with": "nppes_zip",
                "fetch": {"source_url": "https://example.com/nppes.zip"},
                "archive_member": "npidata_pfile*.csv",
                "file_format": "csv",
                "staging_name": "PublicStaging.StagingNppes",
                "public_data_name": "PublicHealthData.NppesNpi",
            },
            {
                "source_name": "pl_pfile",
                "bundled_with": "nppes_zip",
                "archive_member": "pl_pfile*.csv",
                "file_format": "csv",
                "staging_name": "PublicStaging.StagingPl",
                "public_data_name": "PublicHealthData.PlPfile",
            },
            {
                "source_name": "nucc",
                "fetch": {"source_url": "https://example.com/nucc.csv"},
                "file_format": "csv",
                "staging_name": "PublicStaging.StagingNucc",
                "public_data_name": "PublicHealthData.Nucc",
            },
            {
                "source_name": "smd",
                "staging_name": "PublicStaging.StagingSmd",
                "public_data_name": "PublicHealthData.Smd",
                "depends_on": ["nucc"],
            },
            {
                "source_name": "provider",
                "staging_name": "PublicStaging.StagingProvider",
                "public_data_name": "PublicHealthData.Provider",
                "depends_on": ["nppes_npi", "pl_pfile", "smd"],
            },
        ],
    }


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


def test_valid_config_constructs(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    names = [e.source_name for e in reg.entries()]
    assert names == ["nppes_npi", "pl_pfile", "nucc", "smd", "provider"]
    # No fatals recorded on the happy path.
    assert fake_mongo.discrepancies == []


def test_topological_order_puts_derived_after_sources(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    order = [e.source_name for e in reg.topological_order()]
    assert order.index("nucc") < order.index("smd")
    assert order.index("smd") < order.index("provider")
    assert order.index("nppes_npi") < order.index("provider")
    assert order.index("pl_pfile") < order.index("provider")


# ---------------------------------------------------------------------------
# invariant 1: duplicate source_name
# ---------------------------------------------------------------------------


def test_invariant_1_duplicate_source_name(fake_mongo):
    cfg = _baseline_config()
    dup = copy.deepcopy(cfg["dataset_versions"][2])
    dup["staging_name"] = "PublicStaging.OtherNucc"
    dup["public_data_name"] = "PublicHealthData.OtherNucc"
    cfg["dataset_versions"].append(dup)
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_duplicate_source_name"
    assert "nucc" in str(ei.value)
    # fatal recorded before raise
    assert any(d["reason"] == "fatal_dataset_registry_duplicate_source_name"
               for d in fake_mongo.discrepancies)


# ---------------------------------------------------------------------------
# invariant 2: duplicate staging_name
# ---------------------------------------------------------------------------


def test_invariant_2_duplicate_staging_name(fake_mongo):
    cfg = _baseline_config()
    cfg["dataset_versions"][1]["staging_name"] = cfg["dataset_versions"][0]["staging_name"]
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_duplicate_staging_name"
    assert any(d["reason"] == "fatal_dataset_registry_duplicate_staging_name"
               for d in fake_mongo.discrepancies)


# ---------------------------------------------------------------------------
# invariant 3: duplicate public_data_name
# ---------------------------------------------------------------------------


def test_invariant_3_duplicate_public_data_name(fake_mongo):
    cfg = _baseline_config()
    cfg["dataset_versions"][1]["public_data_name"] = cfg["dataset_versions"][0]["public_data_name"]
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_duplicate_public_data_name"
    assert any(d["reason"] == "fatal_dataset_registry_duplicate_public_data_name"
               for d in fake_mongo.discrepancies)


# ---------------------------------------------------------------------------
# invariant 4: bundle fetch count (0 fetchers, 2 fetchers)
# ---------------------------------------------------------------------------


def test_invariant_4_bundle_zero_fetchers(fake_mongo):
    cfg = _baseline_config()
    # Remove fetch from the fetcher, leaving zero fetchers in the bundle.
    del cfg["dataset_versions"][0]["fetch"]
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_bundle_fetch_count"
    assert any(d["reason"] == "fatal_dataset_registry_bundle_fetch_count"
               for d in fake_mongo.discrepancies)


def test_invariant_4_bundle_multiple_fetchers(fake_mongo):
    cfg = _baseline_config()
    cfg["dataset_versions"][1]["fetch"] = {"source_url": "https://example.com/extra.zip"}
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_bundle_fetch_count"


# ---------------------------------------------------------------------------
# invariant 5: dangling dependency
# ---------------------------------------------------------------------------


def test_invariant_5_dangling_dependency(fake_mongo):
    cfg = _baseline_config()
    cfg["dataset_versions"][3]["depends_on"] = ["nucc", "ghost_source"]
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_dangling_dependency"
    assert "ghost_source" in str(ei.value)
    assert any(d["reason"] == "fatal_dataset_registry_dangling_dependency"
               for d in fake_mongo.discrepancies)


# ---------------------------------------------------------------------------
# invariant 6: dependency cycle (message names the cycle path)
# ---------------------------------------------------------------------------


def test_invariant_6_dependency_cycle(fake_mongo):
    cfg = _baseline_config()
    # Introduce cycle: nucc depends on smd (smd already depends on nucc).
    cfg["dataset_versions"][2] = {
        "source_name": "nucc",
        "staging_name": "PublicStaging.StagingNucc",
        "public_data_name": "PublicHealthData.Nucc",
        "depends_on": ["smd"],
    }
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_dependency_cycle"
    msg = str(ei.value)
    # cycle path names the offenders
    assert "nucc" in msg and "smd" in msg
    assert "->" in msg
    assert any(d["reason"] == "fatal_dataset_registry_dependency_cycle"
               for d in fake_mongo.discrepancies)


def test_invariant_6_self_cycle(fake_mongo):
    cfg = _baseline_config()
    cfg["dataset_versions"][3]["depends_on"] = ["smd"]
    with pytest.raises(ChatHealthyException) as ei:
        PipelineDatasetRegistry(cfg, 3, fake_mongo)
    assert ei.value.mode == "dataset_registry_dependency_cycle"
    assert "smd -> smd" in str(ei.value)


# ---------------------------------------------------------------------------
# lookup methods
# ---------------------------------------------------------------------------


def test_by_source_name_returns_entry(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    e = reg.by_source_name("nucc")
    assert isinstance(e, DatasetEntry)
    assert e.staging_db == "PublicStaging"
    assert e.staging_coll_base == "StagingNucc"


def test_by_source_name_unknown_raises(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    with pytest.raises(ChatHealthyException) as ei:
        reg.by_source_name("ghost")
    assert ei.value.mode == "dataset_registry_unknown_source_name"


def test_dependencies_of(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    deps = [d.source_name for d in reg.dependencies_of("provider")]
    assert deps == ["nppes_npi", "pl_pfile", "smd"]
    assert reg.dependencies_of("nucc") == []


def test_fetcher_of_bundle(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    fetcher = reg.fetcher_of_bundle("nppes_zip")
    assert fetcher.source_name == "nppes_npi"


def test_topological_order_method(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    order = [e.source_name for e in reg.topological_order()]
    # Every derived comes after all of its deps.
    for entry in reg.entries():
        if entry.depends_on:
            for dep_name in entry.depends_on:
                assert order.index(dep_name) < order.index(entry.source_name)


def test_staging_collection_name_appends_version(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    assert reg.staging_collection_name("nucc") == "StagingNucc_v_3"


def test_public_data_collection_name_appends_version(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    assert reg.public_data_collection_name("provider") == "Provider_v_3"


def test_is_fetched_bundled_derived(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    assert reg.is_fetched("nppes_npi") is True
    assert reg.is_bundled("nppes_npi") is True
    assert reg.is_derived("nppes_npi") is False
    assert reg.is_fetched("nucc") is True
    assert reg.is_bundled("nucc") is False
    assert reg.is_derived("nucc") is False
    # Class B extractor (pl_pfile) has no own fetch but is bundled.
    assert reg.is_fetched("pl_pfile") is False
    assert reg.is_bundled("pl_pfile") is True
    assert reg.is_derived("pl_pfile") is False
    # Class C derived.
    assert reg.is_fetched("provider") is False
    assert reg.is_bundled("provider") is False
    assert reg.is_derived("provider") is True


def test_resolve_source_url_static(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    assert reg.resolve_source_url("nucc") == "https://example.com/nucc.csv"


def test_resolve_source_url_not_a_fetcher(fake_mongo):
    reg = PipelineDatasetRegistry(_baseline_config(), 3, fake_mongo)
    with pytest.raises(ChatHealthyException) as ei:
        reg.resolve_source_url("provider")
    assert ei.value.mode == "dataset_registry_resolve_url_not_a_fetcher"


def test_resolve_source_url_discovery_calls_out(fake_mongo, monkeypatch):
    cfg = _baseline_config()
    cfg["dataset_versions"][2] = {
        "source_name": "nucc",
        "fetch": {"source_url": {"page_url": "https://example.com/", "instructions": "Find X"}},
        "file_format": "csv",
        "staging_name": "PublicStaging.StagingNucc",
        "public_data_name": "PublicHealthData.Nucc",
    }
    calls = {}

    def _fake_find(source_name, page_url, instructions):
        calls["source_name"] = source_name
        calls["page_url"] = page_url
        calls["instructions"] = instructions
        return "https://example.com/latest.csv"

    import source_url_discovery
    monkeypatch.setattr(source_url_discovery, "find_latest_data_url", _fake_find)

    reg = PipelineDatasetRegistry(cfg, 3, fake_mongo)
    url = reg.resolve_source_url("nucc")
    assert url == "https://example.com/latest.csv"
    assert calls["source_name"] == "nucc"
    assert calls["page_url"] == "https://example.com/"
