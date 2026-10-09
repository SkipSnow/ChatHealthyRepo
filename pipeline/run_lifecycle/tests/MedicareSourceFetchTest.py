# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""MedicareSourceFetchTest -- one end-to-end test that drives the pydantic-ai
data-fetch agent to fetch EVERY Medicare pipeline source, to disk, in full, and
verifies the stored bytes.

For each source a FetchRequest is handed to the agent's run() -- which runs the
pydantic-ai Agent through the ChatHealthy LLM facade -- and the file it stores is
inspected: it must be the expected kind (zip / text) and must NOT be an HTML
error/landing page (a 200 that returns a landing page would otherwise pass size
and sha checks while being garbage). Nothing is gated or partial: production
pulls the whole file, so the test does too.

Needs CH_URL_DISCOVERY_MODEL (the agent's 'provider:model') set in the
environment; the agent runs the model on every fetch.
"""
import hashlib
import os

os.environ.setdefault("CH_LOG_DESTINATION", "stderr")
os.environ.setdefault("CH_SPACE_NAME", "test")
os.environ.setdefault("CH_COMPONENT", "test")
os.environ.setdefault("ENV_PREFIX", "local")

import pytest  # noqa: E402

from pipeline.run_lifecycle.data_fetch_agent import (  # noqa: E402
    DirectUrlFind, DkanCatalogFind, FetchRequest, FileStore, JsonManifestFind,
    StaticPageLatestFind, run)

_DATA_JSON = "https://data.cms.gov/data.json"
_BUFFER = 1 << 20


def _assert_content(name: str, path: str, expected_kind: str) -> None:
    """The file on disk must be the real data, not an HTML error/landing page."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    lowered = head.lstrip()[:300].lower()
    assert b"<html" not in lowered and b"<!doctype" not in lowered, \
        f"{name}: stored an HTML page, not {expected_kind} (head={head[:60]!r})"
    if expected_kind == "zip":
        assert head[:2] == b"PK", f"{name}: not a ZIP (head={head[:8]!r})"
    elif expected_kind == "text":
        assert head[:2] != b"PK", f"{name}: expected text, got a ZIP"
        assert any(d in head for d in (b",", b"|", b"\t")), \
            f"{name}: text has no delimiter (head={head[:60]!r})"
    else:  # pragma: no cover
        pytest.fail(f"unknown expected_kind {expected_kind!r}")


# (name, find spec, expected kind) -- every Medicare pipeline source.
_MEDICARE_SOURCES = [
    ("medicare_partb", DkanCatalogFind(
        catalog_url=_DATA_JSON,
        dataset_title="Medicare Physician & Other Practitioners - by Provider and Service"),
     "text"),
    ("medicare_partd", DkanCatalogFind(
        catalog_url=_DATA_JSON,
        dataset_title="Medicare Part D Prescribers - by Provider and Drug"),
     "text"),
    ("medicare_pos", DkanCatalogFind(
        catalog_url=_DATA_JSON,
        dataset_title="Provider of Services File - Quality Improvement and Evaluation System"),
     "text"),
    ("openfda_labels", JsonManifestFind(
        manifest_url="https://api.fda.gov/download.json",
        json_path=["results", "drug", "label", "partitions", 0, "file"]),
     "zip"),
    ("ccsr", StaticPageLatestFind(
        page_url="https://hcup-us.ahrq.gov/toolssoftware/ccsr/dxccsr.jsp",
        link_suffix=".zip"),
     "zip"),
    ("icd10_cm", StaticPageLatestFind(
        page_url="https://www.cms.gov/medicare/coding-billing/icd-10-codes",
        link_suffix="-code-descriptions-tabular-order.zip"),
     "zip"),
    ("medicare_coverage_hcpc", DirectUrlFind(
        url="https://downloads.cms.gov/medicare-coverage-database/downloads/exports/all_article.zip"),
     "zip"),
]


@pytest.mark.skipif(not os.environ.get("CH_URL_DISCOVERY_MODEL"),
                    reason="needs CH_URL_DISCOVERY_MODEL (the agent's provider:model)")
def test_agent_fetches_all_medicare_sources(tmp_path):
    for name, find, kind in _MEDICARE_SOURCES:
        dest = str(tmp_path / name / "payload.bin")
        result = run(FetchRequest(source_name=name, find=find,
                                  store=FileStore(path=dest), stream=True,
                                  buffer_size=_BUFFER))
        assert os.path.exists(dest), f"{name}: agent did not write a file"
        on_disk = os.path.getsize(dest)
        assert result.size_bytes == on_disk and on_disk > 200, f"{name}: bad size {on_disk}"
        _assert_content(name, dest, kind)
        # Re-hash from disk in bounded chunks (the giants are multi-GB) and
        # confirm it matches what the agent reported, then free the bytes.
        h = hashlib.sha256()
        with open(dest, "rb") as fh:
            for chunk in iter(lambda: fh.read(_BUFFER), b""):
                h.update(chunk)
        assert result.sha256 == h.hexdigest(), f"{name}: sha mismatch"
        os.remove(dest)
