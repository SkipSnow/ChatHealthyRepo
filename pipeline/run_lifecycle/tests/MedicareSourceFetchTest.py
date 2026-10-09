# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""MedicareSourceFetchTest -- the data-fetch agent used the way a user uses it,
for every fetchable Medicare source, with the stored file CONTENT verified.

A user builds a FetchRequest and calls run(), which does find -> download ->
store. Every source here is driven through run() to a real file on disk, and
the stored bytes are inspected: the file must be the expected kind (zip / text /
json) and must NOT be an HTML page -- a 200 that returns an error/landing page
would otherwise pass size/sha checks while being garbage.

Scope:
  * pl_pfile, medicare_coverage_icd10 are members ETL extracts from the
    nppes / coverage zips -- not user fetches, not listed.
  * partb / partd / nppes_npi are multi-GB; gated behind RUN_GIANT_FETCH=1.
  * specialty_catalog needs CLOUDFLARE_PIPELINE_AUTH_HEADER (the pipeline header).
  * llm_discovery sources need CH_URL_DISCOVERY_MODEL.
  * medicare_pos has no runnable find yet (dataset title absent from the live
    catalog) -- the one explicit gap.
"""
import hashlib
import http.server
import os
import socketserver
import threading

os.environ.setdefault("CH_LOG_DESTINATION", "stderr")
os.environ.setdefault("CH_SPACE_NAME", "test")
os.environ.setdefault("CH_COMPONENT", "test")
os.environ.setdefault("ENV_PREFIX", "local")

import pytest  # noqa: E402

from pipeline.run_lifecycle.data_fetch_agent import (  # noqa: E402
    DirectUrlFind, DkanCatalogFind, FetchRequest, FileStore, JsonManifestFind,
    LlmDiscoveryFind, StaticPageLatestFind, run)

_DATA_JSON = "https://data.cms.gov/data.json"
_BUFFER = 1 << 20


def _assert_content(name: str, path: str, expected_kind: str) -> None:
    """The file on disk must be the real data, not an HTML error/landing page."""
    with open(path, "rb") as fh:
        head = fh.read(512)
    stripped = head.lstrip()
    assert b"<html" not in stripped[:300].lower() and \
        b"<!doctype" not in stripped[:300].lower(), \
        f"{name}: stored an HTML page, not {expected_kind} (head={head[:60]!r})"
    if expected_kind == "zip":
        assert head[:2] == b"PK", f"{name}: not a ZIP (head={head[:8]!r})"
    elif expected_kind == "json":
        assert stripped[:1] in (b"{", b"["), f"{name}: not JSON (head={head[:40]!r})"
    elif expected_kind == "text":
        assert head[:2] != b"PK", f"{name}: expected text, got a ZIP"
        assert any(d in head for d in (b",", b"|", b"\t")), \
            f"{name}: text has no delimiter (head={head[:60]!r})"
    else:  # pragma: no cover
        pytest.fail(f"unknown expected_kind {expected_kind!r}")


# =========================================================================== #
# Hermetic mechanics -- real agent run(), local server, file store.           #
# =========================================================================== #
_PAYLOAD = b"npi,hcpcs,allowed\n" + b"1234567890,99213,123.45\n" * 10_000


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Length", str(len(_PAYLOAD)))
        self.end_headers()
        self.wfile.write(_PAYLOAD)

    def log_message(self, *args):
        return


@pytest.fixture()
def file_url():
    httpd = socketserver.TCPServer(("127.0.0.1", 0), _Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{port}/MUP_PHY.csv"
    finally:
        httpd.shutdown()


@pytest.mark.parametrize("stream,buffer_size", [(True, 4096), (False, None)])
def test_mechanics_run_to_file(file_url, tmp_path, stream, buffer_size):
    dest = str(tmp_path / "sub" / "out.csv")
    result = run(FetchRequest(
        source_name="mechanics", find=DirectUrlFind(url=file_url),
        store=FileStore(path=dest), stream=stream, buffer_size=buffer_size))
    with open(dest, "rb") as fh:
        assert fh.read() == _PAYLOAD
    assert result.sha256 == hashlib.sha256(_PAYLOAD).hexdigest()
    assert result.size_bytes == len(_PAYLOAD)


# =========================================================================== #
# Every fetchable Medicare source: run() -> disk -> verify content.           #
# (name, find, expected_kind, giant, needs_cf, needs_llm)                     #
# =========================================================================== #
_SOURCES = [
    ("medicare_partb", DkanCatalogFind(
        catalog_url=_DATA_JSON,
        dataset_title="Medicare Physician & Other Practitioners - by Provider and Service"),
     "text", True, False, False),
    ("medicare_partd", DkanCatalogFind(
        catalog_url=_DATA_JSON,
        dataset_title="Medicare Part D Prescribers - by Provider and Drug"),
     "text", True, False, False),
    ("icd10_cm", StaticPageLatestFind(
        page_url="https://www.cms.gov/medicare/coding-billing/icd-10-codes",
        link_suffix="-code-descriptions-tabular-order.zip"),
     "zip", False, False, False),
    ("openfda_labels", JsonManifestFind(
        manifest_url="https://api.fda.gov/download.json",
        json_path=["results", "drug", "label", "partitions", 0, "file"]),
     "zip", False, False, False),
    ("ccsr", StaticPageLatestFind(
        page_url="https://hcup-us.ahrq.gov/toolssoftware/ccsr/dxccsr.jsp",
        link_suffix=".zip"),
     "zip", False, False, False),
    ("specialty_catalog", DirectUrlFind(
        url="https://chathealthy.ai/Data/specialty_classification_gpt41.json"),
     "json", False, True, False),
    ("nppes_npi", LlmDiscoveryFind(
        page_url="https://download.cms.gov/nppes/NPI_Files.html",
        instructions="Find the URL of the latest full monthly NPPES NPI Dissemination "
                     "zip (name starts NPPES_Data_Dissemination_<Month>_<Year>, ends .zip; "
                     "highest version for the latest month; ignore Weekly and Deactivated)."),
     "zip", True, False, True),
    ("medicare_coverage_hcpc", DirectUrlFind(
        url="https://downloads.cms.gov/medicare-coverage-database/downloads/exports/all_article.zip"),
     "zip", False, False, False),
    ("medicare_pos", DkanCatalogFind(
        catalog_url=_DATA_JSON,
        dataset_title="Provider of Services File - Quality Improvement and Evaluation System"),
     "text", False, False, False),
    ("nucc", LlmDiscoveryFind(
        page_url="https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40/csv-mainmenu-57",
        instructions="Find the URL of the latest NUCC Provider Taxonomy CSV file."),
     "text", False, False, True),
    ("census_zcta_county", LlmDiscoveryFind(
        page_url="https://www2.census.gov/geo/docs/maps-data/data/rel2020/zcta520/",
        instructions="Find the latest US Census ZCTA-to-County relationship pipe-delimited TXT."),
     "text", False, False, True),
    ("usda_rucc", LlmDiscoveryFind(
        page_url="https://www.ers.usda.gov/data-products/rural-urban-continuum-codes/",
        instructions="Find the latest USDA Rural-Urban Continuum Codes spreadsheet (xlsx)."),
     "zip", False, False, True),
]


@pytest.mark.parametrize("name,find,expected_kind,giant,needs_cf,needs_llm",
                         _SOURCES, ids=[s[0] for s in _SOURCES])
def test_source_end_to_end(name, find, expected_kind, giant, needs_cf, needs_llm, tmp_path):
    # Every source downloads in FULL -- production has no partial fetch, so the
    # test has none. `giant` is informational only (which sources are multi-GB).
    if needs_cf and not os.environ.get("CLOUDFLARE_PIPELINE_AUTH_HEADER"):
        pytest.skip(f"{name}: needs CLOUDFLARE_PIPELINE_AUTH_HEADER (Cloudflare bot rule)")
    if needs_llm and not os.environ.get("CH_URL_DISCOVERY_MODEL"):
        pytest.skip(f"{name}: needs CH_URL_DISCOVERY_MODEL for llm_discovery")

    headers = None
    if needs_cf:
        headers = {"X-ChatHealthy-Pipeline-Auth": os.environ["CLOUDFLARE_PIPELINE_AUTH_HEADER"]}
    dest = str(tmp_path / name / "payload.bin")

    result = run(FetchRequest(
        source_name=name, find=find, store=FileStore(path=dest),
        stream=True, buffer_size=_BUFFER), headers=headers)

    assert os.path.exists(dest), f"{name}: agent did not write a file"
    on_disk = os.path.getsize(dest)
    assert result.size_bytes == on_disk and on_disk > 200, f"{name}: bad size {on_disk}"
    _assert_content(name, dest, expected_kind)
    # Re-hash from disk in bounded chunks (the giants are multi-GB) and confirm
    # it matches what the agent reported; then free the bytes so sequential
    # multi-GB downloads don't accumulate.
    h = hashlib.sha256()
    with open(dest, "rb") as fh:
        for chunk in iter(lambda: fh.read(_BUFFER), b""):
            h.update(chunk)
    assert result.sha256 == h.hexdigest(), f"{name}: sha mismatch"
    os.remove(dest)
