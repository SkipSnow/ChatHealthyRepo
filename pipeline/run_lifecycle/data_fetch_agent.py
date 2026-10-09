# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""data_fetch_agent.py -- the one data-fetching agent for every pipeline source.

One agent, one entry (run). Its typed input branches by mode:

  find   HOW to find the file's URL (the pointer):
           direct_url          a stable constant URL.                   [det.]
           dkan_catalog        latest downloadURL from a DKAN data.json
                               catalog, by dataset title.               [det.]
           static_page_latest  latest versioned link on a static page,
                               by a declared suffix.                    [det.]
           llm_discovery       an LLM reads the page and returns a URL
                               when the pointer is not deterministic.   [LLM]

  store  WHERE to put the bytes: file (just a path) or blob (account+auth
         via a connection string, plus container and blob path).

  stream whether to stream the bytes (affects download AND store). A switch:
         true  -> the response streams straight to the store, chunk by chunk;
         false -> the whole file is swallowed into memory and written once.
         Never a temp disk file either way; true needs a buffer_size.

The fetch is deterministic -- download and store never involve the LLM; only
llm_discovery asks the model for the pointer. The agent fetches no secrets
(the caller populates the request) and never reads the content it stores --
ETL parses it downstream.
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from typing import Annotated, Literal, Union
from urllib.parse import urljoin, urlparse

import requests
from pydantic import BaseModel, Field

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

_log = ChatHealthyLoggingService()

_DEFAULT_TIMEOUT = 600
_UA = "ChatHealthy-Pipeline/1.0"


# --- find spec: branches by mode; each carries only what it needs ----------- #
class DirectUrlFind(BaseModel):
    mode: Literal["direct_url"] = "direct_url"
    url: str


class DkanCatalogFind(BaseModel):
    mode: Literal["dkan_catalog"] = "dkan_catalog"
    catalog_url: str
    dataset_title: str
    media_type: str = "text/csv"


class StaticPageLatestFind(BaseModel):
    mode: Literal["static_page_latest"] = "static_page_latest"
    page_url: str
    link_suffix: str


class JsonManifestFind(BaseModel):
    mode: Literal["json_manifest"] = "json_manifest"
    manifest_url: str
    json_path: list[Union[str, int]]      # keys/indices to walk to the URL string


class LlmDiscoveryFind(BaseModel):
    mode: Literal["llm_discovery"] = "llm_discovery"
    page_url: str
    instructions: str = ""


FindSpec = Annotated[
    Union[DirectUrlFind, DkanCatalogFind, StaticPageLatestFind, JsonManifestFind,
          LlmDiscoveryFind],
    Field(discriminator="mode"),
]


# --- store spec: branches by mode; file needs only a path, blob needs the
#     account+auth (connection string) and container/path -------------------- #
class FileStore(BaseModel):
    mode: Literal["file"] = "file"
    path: str


class BlobStore(BaseModel):
    mode: Literal["blob"] = "blob"
    connection_string: str                # account + auth; the caller fetches it
    container: str
    blob_path: str


StoreSpec = Annotated[Union[FileStore, BlobStore], Field(discriminator="mode")]


class FetchRequest(BaseModel):
    """Typed INPUT. find and store each branch by mode -- a file store carries
    no blob fields, a blob store carries the account+auth and container/path.
    The caller populates everything (including any secret it fetched)."""
    source_name: str
    find: FindSpec
    store: StoreSpec
    stream: bool                          # stream the bytes (affects fetch + store)
    buffer_size: int | None = None        # required when stream=True: chunk size in bytes


class FindResult(BaseModel):
    """What FIND produces -- the pointer facts. OUTPUT only."""
    url: str
    version_identifier: str | None = None
    filename: str | None = None
    published_date: str | None = None


class FetchResult(BaseModel):
    """Typed OUTPUT: the find facts plus what was stored."""
    source_name: str
    url: str
    version_identifier: str | None = None
    filename: str | None = None
    published_date: str | None = None
    sha256: str
    size_bytes: int
    stored: dict


def _basename(url: str) -> str:
    return os.path.basename(urlparse(url).path) or ""


# --------------------------------------------------------------------------- #
# FIND -- answer where, by the find branch. Returns the pointer facts.         #
# --------------------------------------------------------------------------- #
def _find_direct_url(source_name: str, spec: DirectUrlFind) -> FindResult:
    return FindResult(url=spec.url, filename=_basename(spec.url))


def _find_dkan_catalog(source_name: str, spec: DkanCatalogFind) -> FindResult:
    title = spec.dataset_title.strip()
    r = urllib.request.Request(spec.catalog_url, headers={"User-Agent": _UA,
                                                          "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=_DEFAULT_TIMEOUT) as resp:
        catalog = json.loads(resp.read().decode("utf-8", "replace"))
    datasets = catalog.get("dataset") or []

    def _edition(d: dict) -> str:
        t = d.get("title") or ""
        return t.split(" : ", 1)[1].strip() if " : " in t else ""

    def _base(d: dict) -> str:
        return (d.get("title") or "").split(" : ", 1)[0].strip()

    matches = [d for d in datasets if _base(d) == title]
    if not matches:
        raise ChatHealthyException(
            mode="discovery_no_match", component="data_fetch_agent",
            message=(f"data_fetch_agent[{source_name}]: no dataset titled "
                     f"{title!r} in {spec.catalog_url}"))
    chosen = max(matches, key=_edition)
    dists = chosen.get("distribution") or []
    url = next((d.get("downloadURL") for d in dists
                if d.get("mediaType") == spec.media_type and d.get("downloadURL")), None)
    if not url:
        url = next((d.get("downloadURL") for d in dists if d.get("downloadURL")), None)
    if not url:
        raise ChatHealthyException(
            mode="discovery_no_match", component="data_fetch_agent",
            message=(f"data_fetch_agent[{source_name}]: dataset {title!r} edition "
                     f"{_edition(chosen)!r} has no distribution downloadURL"))
    edition = _edition(chosen) or None
    _log.LogPipeline("INFO", "data_fetch_agent[%s]: dkan_catalog edition=%s -> %s",
                     source_name, edition, url)
    return FindResult(url=url, version_identifier=edition, filename=_basename(url),
                      published_date=edition)


def _find_static_page_latest(source_name: str, spec: StaticPageLatestFind) -> FindResult:
    suffix = spec.link_suffix.lower()
    resp = requests.get(spec.page_url, timeout=_DEFAULT_TIMEOUT, headers={"User-Agent": _UA})
    resp.raise_for_status()
    from bs4 import BeautifulSoup  # noqa: PLC0415
    soup = BeautifulSoup(resp.text, "html.parser")
    candidates = [(a.get("href") or "").strip() for a in soup.find_all("a")
                  if (a.get("href") or "").strip().lower().endswith(suffix)]
    if not candidates:
        raise ChatHealthyException(
            mode="discovery_no_match", component="data_fetch_agent",
            message=(f"data_fetch_agent[{source_name}]: no link ending {suffix!r} "
                     f"on {spec.page_url}"))

    def _token(href: str) -> str:
        return os.path.basename(urlparse(href).path).split("-", 1)[0]

    best_href = max(candidates, key=_token)
    url = urljoin(spec.page_url, best_href)
    _log.LogPipeline("INFO", "data_fetch_agent[%s]: static_page_latest -> %s",
                     source_name, url)
    return FindResult(url=url, version_identifier=_token(best_href) or None,
                      filename=_basename(url))


def _find_json_manifest(source_name: str, spec: JsonManifestFind) -> FindResult:
    """Deterministic: fetch a JSON manifest and walk a declared key/index path
    to the download URL (e.g. openFDA download.json ->
    results.drug.label.partitions[0].file). No LLM -- the pointer is a
    deterministic lookup in structured JSON."""
    r = urllib.request.Request(spec.manifest_url, headers={"User-Agent": _UA,
                                                           "Accept": "application/json"})
    with urllib.request.urlopen(r, timeout=_DEFAULT_TIMEOUT) as resp:
        doc = json.loads(resp.read().decode("utf-8", "replace"))
    node = doc
    for key in spec.json_path:
        try:
            node = node[key]
        except (KeyError, IndexError, TypeError) as exc:
            raise ChatHealthyException(
                mode="discovery_no_match", component="data_fetch_agent",
                message=(f"data_fetch_agent[{source_name}]: json_manifest path "
                         f"{spec.json_path} failed at {key!r}: {exc}"),
                exception=exc) from exc
    if not isinstance(node, str) or not node.startswith("http"):
        raise ChatHealthyException(
            mode="discovery_no_match", component="data_fetch_agent",
            message=(f"data_fetch_agent[{source_name}]: json_manifest path "
                     f"{spec.json_path} did not resolve to a URL (got {node!r})"))
    _log.LogPipeline("INFO", "data_fetch_agent[%s]: json_manifest -> %s",
                     source_name, node)
    return FindResult(url=node, filename=_basename(node))


def _find_llm_discovery(source_name: str, spec: LlmDiscoveryFind) -> FindResult:
    from pipeline.run_lifecycle.source_url_discovery import find_latest_source_version  # noqa: PLC0415
    facts = find_latest_source_version(source_name=source_name, page_url=spec.page_url,
                                       instructions=spec.instructions or "")
    return FindResult(url=facts["url"], version_identifier=facts.get("version_identifier"),
                      filename=facts.get("filename"), published_date=facts.get("published_date"))


_FIND = {
    "direct_url": _find_direct_url,
    "dkan_catalog": _find_dkan_catalog,
    "static_page_latest": _find_static_page_latest,
    "json_manifest": _find_json_manifest,
    "llm_discovery": _find_llm_discovery,
}


# --------------------------------------------------------------------------- #
# Acquire -- response straight to the store, OR whole into memory then store.  #
# A switch (stream). NEVER a temp disk file. Never reads the content.          #
# --------------------------------------------------------------------------- #
def _acquire(url: str, *, source_name: str, store: Union[FileStore, BlobStore],
             stream: bool, buffer_size: int, headers: dict | None,
             http_timeout: int) -> tuple[str, int, dict]:
    hdrs = {"User-Agent": _UA}
    if headers:
        hdrs.update(headers)
    hasher = hashlib.sha256()

    if isinstance(store, FileStore):
        os.makedirs(os.path.dirname(os.path.abspath(store.path)) or ".", exist_ok=True)
        size = 0
        resp = requests.get(url, stream=stream, timeout=http_timeout, headers=hdrs)
        resp.raise_for_status()
        with open(store.path, "wb") as dst:
            if stream:                           # response -> file, chunk by chunk
                for chunk in resp.iter_content(chunk_size=buffer_size):
                    if not chunk:
                        continue
                    dst.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            else:                                # whole into memory, write once
                data = resp.content
                dst.write(data)
                hasher.update(data)
                size = len(data)
        _log.LogPipeline("INFO", "data_fetch_agent[%s]: file %s (%d bytes) stream=%s",
                         source_name, store.path, size, stream)
        return hasher.hexdigest(), size, {
            "store_mode": "file", "path": store.path, "size_bytes": size}

    # BlobStore -- build the client from the connection string the caller put
    # in the request; the agent fetches no secret of its own.
    from azure.storage.blob import BlobServiceClient  # noqa: PLC0415
    svc = BlobServiceClient.from_connection_string(store.connection_string)
    cc = svc.get_container_client(store.container)
    try:
        cc.create_container()
    except Exception:  # noqa: BLE001 -- already exists
        pass
    bc = cc.get_blob_client(store.blob_path)
    resp = requests.get(url, stream=stream, timeout=http_timeout, headers=hdrs)
    resp.raise_for_status()
    if stream:                                   # response -> blob, chunk by chunk
        counter = {"n": 0}

        def _hashing_chunks():
            for chunk in resp.iter_content(chunk_size=buffer_size):
                if not chunk:
                    continue
                hasher.update(chunk)
                counter["n"] += len(chunk)
                yield chunk

        bc.upload_blob(_hashing_chunks(), overwrite=True, length=None)
        size = counter["n"]
    else:                                        # whole into memory, upload once
        data = resp.content
        hasher.update(data)
        size = len(data)
        bc.upload_blob(data, overwrite=True)
    _log.LogPipeline("INFO", "data_fetch_agent[%s]: blob %s/%s (%d bytes) stream=%s",
                     source_name, store.container, store.blob_path, size, stream)
    return hasher.hexdigest(), size, {
        "store_mode": "blob", "container": store.container,
        "blob_path": store.blob_path, "size_bytes": size}


def find_url(source_name: str, find_spec) -> str:
    """Resolve only the pointer (URL) for a source via its find mode. Used by
    callers that do their own download/store but want the agent's deterministic
    (or llm_discovery) WHERE. Never downloads."""
    finder = _FIND.get(find_spec.mode)
    if finder is None:
        raise ChatHealthyException(
            mode="runtime_error", component="data_fetch_agent",
            message=f"data_fetch_agent[{source_name}]: unknown find mode {find_spec.mode!r}")
    return finder(source_name, find_spec).url


# --------------------------------------------------------------------------- #
# The data-fetching agent -- one entry: find -> acquire (stream|memory).       #
# --------------------------------------------------------------------------- #
def run(request: FetchRequest, *, headers: dict | None = None,
        http_timeout: int = _DEFAULT_TIMEOUT) -> FetchResult:
    """Run the agent: FIND the url by the find branch, then acquire the bytes to
    the store -- streamed chunk-by-chunk when request.stream, else swallowed
    whole into memory and written once. No temp disk. The caller populates the
    request (including any blob connection string it fetched); the agent fetches
    no secrets. Deterministic except the llm_discovery find mode."""
    if request.stream and not request.buffer_size:
        raise ChatHealthyException(
            mode="runtime_error", component="data_fetch_agent",
            message=(f"data_fetch_agent[{request.source_name}]: stream=True requires "
                     f"a buffer_size"))
    buffer_size = request.buffer_size or (1024 * 1024)
    finder = _FIND.get(request.find.mode)
    if finder is None:
        raise ChatHealthyException(
            mode="runtime_error", component="data_fetch_agent",
            message=(f"data_fetch_agent[{request.source_name}]: unknown find mode "
                     f"{request.find.mode!r}"))
    found = finder(request.source_name, request.find)
    sha256, size, stored = _acquire(
        found.url, source_name=request.source_name, store=request.store,
        stream=request.stream, buffer_size=buffer_size, headers=headers,
        http_timeout=http_timeout)
    return FetchResult(source_name=request.source_name, url=found.url,
                       version_identifier=found.version_identifier,
                       filename=found.filename, published_date=found.published_date,
                       sha256=sha256, size_bytes=size, stored=stored)
