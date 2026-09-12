# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Registry-coupling contract for zip_county_crosswalk_loader.CrosswalkFetcher.

Proves CrosswalkFetcher routes source_url resolution through
PipelineDatasetRegistry.resolve_source_url("census_zcta_county") -- if the
dataset_versions[] entry for census_zcta_county shifts its fetch.source_url
in the brain JSON, the fetcher MUST follow it. The fetcher no longer carries
a hardcoded discovery page_url of its own.
"""

from __future__ import annotations

import pytest

from chathealthy_lib.exceptions import ChatHealthyException


class _FakeColl:
    def insert_one(self, doc):
        pass


class _FakeDb:
    def __getitem__(self, name):
        return _FakeColl()


class _FakeMongo:
    def __getitem__(self, name):
        return _FakeDb()


def _registry_with_discovery(monkeypatch, discovered_url: str):
    """Build a registry whose census_zcta_county entry uses a discovery
    source_url, and stub source_url_discovery.find_latest_data_url so no
    network call fires."""
    from pipeline_dataset_registry import PipelineDatasetRegistry

    cfg = {
        "dataset_versions": [
            {
                "source_name": "census_zcta_county",
                "fetch": {
                    "source_url": {
                        "page_url": "https://example.com/zcta/",
                        "instructions": "Find the latest national ZCTA-to-county file.",
                    }
                },
                "file_format": "pipe_delimited",
                "staging_name": "PublicStaging.StagingCensusZctaCounty",
                "public_data_name": "PipelinePublicHealthData.CensusZctaCounty",
            },
        ],
    }
    calls = {}

    def _fake_find(source_name, page_url, instructions):
        calls["source_name"] = source_name
        calls["page_url"] = page_url
        calls["instructions"] = instructions
        return discovered_url

    import source_url_discovery
    monkeypatch.setattr(source_url_discovery, "find_latest_data_url", _fake_find)
    return PipelineDatasetRegistry(cfg, 3, _FakeMongo()), calls


@pytest.mark.unit
def test_crosswalk_fetcher_requires_registry():
    from zip_county_crosswalk_loader import CrosswalkFetcher
    with pytest.raises(ChatHealthyException) as ei:
        CrosswalkFetcher({})
    assert ei.value.mode == "pipeline_config_missing_field"


@pytest.mark.unit
def test_crosswalk_fetcher_resolves_url_via_registry(monkeypatch):
    from zip_county_crosswalk_loader import CrosswalkFetcher
    reg, calls = _registry_with_discovery(
        monkeypatch,
        "https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/tab20_zcta520_county20_natl.txt",
    )
    fetcher = CrosswalkFetcher({}, registry=reg)
    resolved = fetcher._resolve_source_url()
    assert resolved.endswith("tab20_zcta520_county20_natl.txt")
    assert calls["source_name"] == "census_zcta_county"
    assert calls["page_url"] == "https://example.com/zcta/"
    assert "national" in calls["instructions"].lower()


@pytest.mark.unit
def test_load_crosswalk_requires_registry():
    from zip_county_crosswalk_loader import load_crosswalk
    with pytest.raises(ChatHealthyException) as ei:
        load_crosswalk({})
    assert ei.value.mode == "pipeline_config_missing_field"


@pytest.mark.unit
def test_no_hardcoded_census_page_url_constant_left():
    # The pre-refactor _CENSUS_ZCTA_INDEX_URL module constant MUST be gone;
    # its only legitimate home is now brain/machine_artifacts/content/
    # pipeline_config.json under the census_zcta_county entry.
    import zip_county_crosswalk_loader as loader
    assert not hasattr(loader, "_CENSUS_ZCTA_INDEX_URL")
