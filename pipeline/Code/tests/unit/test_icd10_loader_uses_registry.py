# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Registry-coupling contract for icd10_loader.Icd10Fetcher.

Proves Icd10Fetcher routes source_url resolution through
PipelineDatasetRegistry.resolve_source_url("icd10_cm") -- if the
dataset_versions[] entry for icd10_cm shifts its fetch.source_url in the
brain JSON, the fetcher MUST follow it. All three prior module-level URL
constants (ICD10_BASE_URL, ICD10_FALLBACK_URL, ICD10_INDEX_URL) and the
_discover_icd10_url() helper MUST be gone.
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
    from pipeline_dataset_registry import PipelineDatasetRegistry

    cfg = {
        "dataset_versions": [
            {
                "source_name": "icd10_cm",
                "fetch": {
                    "source_url": {
                        "page_url": "https://example.com/cms/icd10/",
                        "instructions": "Find the latest CMS ICD-10-CM code-descriptions-tabular-order zip.",
                    }
                },
                "file_format": "zip_containing_csv",
                "staging_name": "PublicStaging.StagingIcd10Cm",
                "public_data_name": "PipelinePublicHealthData.Icd10Cm",
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
def test_icd10_fetcher_requires_registry():
    from icd10_loader import Icd10Fetcher
    with pytest.raises(ChatHealthyException) as ei:
        Icd10Fetcher({})
    assert ei.value.mode == "pipeline_config_missing_field"


@pytest.mark.unit
def test_icd10_fetcher_resolves_url_via_registry(monkeypatch):
    from icd10_loader import Icd10Fetcher
    reg, calls = _registry_with_discovery(
        monkeypatch,
        "https://www.cms.gov/files/zip/april-1-2026-code-descriptions-tabular-order.zip",
    )
    fetcher = Icd10Fetcher({}, registry=reg)
    resolved = fetcher._resolve_source_url()
    assert resolved.endswith("code-descriptions-tabular-order.zip")
    assert calls["source_name"] == "icd10_cm"
    assert calls["page_url"] == "https://example.com/cms/icd10/"
    assert "code-descriptions-tabular-order" in calls["instructions"].lower()


@pytest.mark.unit
def test_load_icd10_requires_registry():
    from icd10_loader import load_icd10
    with pytest.raises(ChatHealthyException) as ei:
        load_icd10({})
    assert ei.value.mode == "pipeline_config_missing_field"


@pytest.mark.unit
def test_no_hardcoded_icd10_constants_left():
    # All three module-level URL constants + the _discover_icd10_url helper
    # MUST be gone; the icd10_cm entry in pipeline_config.json is now the
    # single source of truth for the ICD-10 source URL.
    import icd10_loader as loader
    assert not hasattr(loader, "ICD10_BASE_URL")
    assert not hasattr(loader, "ICD10_FALLBACK_URL")
    assert not hasattr(loader, "ICD10_INDEX_URL")
    assert not hasattr(loader, "_discover_icd10_url")
