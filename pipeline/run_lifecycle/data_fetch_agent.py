# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""data_fetch_agent.py -- the one data-fetching agent for every pipeline source.

A real pydantic-ai Agent. Input is FetchRequest, output is FetchResult, and the
agent runs the pydantic-ai model directly (agent.run_sync) on every fetch -- no
facade. It has one tool, fetch_source, which is the agent's output: the
model produces its result by calling it. For a non-deterministic pointer the
model reads the index page and passes the URL it found; for every other find
mode the tool resolves the URL itself. The tool downloads and stores the bytes
and returns the FetchResult -- the bytes never pass through the model, and the
agent fetches no secrets (the caller populates the request).
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
from pydantic_ai import Agent, RunContext

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

_log = ChatHealthyLoggingService()

_DEFAULT_TIMEOUT = 600
_UA = "ChatHealthy-Pipeline/1.0"
_PAGE_HTML_CHARS = 32_000
_URL_FORBIDDEN = frozenset(" \t\r\n'\"<>")


# --- find spec: branches by mode -------------------------------------------- #
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
    json_path: list[Union[str, int]]


class LlmDiscoveryFind(BaseModel):
    mode: Literal["llm_discovery"] = "llm_discovery"
    page_url: str
    instructions: str = ""


FindSpec = Annotated[
    Union[DirectUrlFind, DkanCatalogFind, StaticPageLatestFind, JsonManifestFind,
          LlmDiscoveryFind],
    Field(discriminator="mode"),
]


# --- store spec: branches by mode ------------------------------------------- #
class FileStore(BaseModel):
    mode: Literal["file"] = "file"
    path: str


class BlobStore(BaseModel):
    mode: Literal["blob"] = "blob"
    connection_string: str
    container: str
    blob_path: str


StoreSpec = Annotated[Union[FileStore, BlobStore], Field(discriminator="mode")]


class FetchRequest(BaseModel):
    """Typed INPUT. find and store each branch by mode. The caller populates
    everything (including any secret it fetched)."""
    source_name: str
    find: FindSpec
    store: StoreSpec
    stream: bool
    buffer_size: int | None = None


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


def _is_valid_url(value: str) -> bool:
    for scheme in ("https://", "http://"):
        if value.startswith(scheme):
            rest = value[len(scheme):]
            return bool(rest) and not any(c in _URL_FORBIDDEN for c in rest)
    return False


# --- deterministic finders -- each answers WHERE for one mode. No model. ----- #
class _FindFacts(BaseModel):
    url: str
    version_identifier: str | None = None
    filename: str | None = None
    published_date: str | None = None


def _find_direct_url(source_name: str, spec: DirectUrlFind) -> _FindFacts:
    return _FindFacts(url=spec.url, filename=_basename(spec.url))


def _find_dkan_catalog(source_name: str, spec: DkanCatalogFind) -> _FindFacts:
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
    return _FindFacts(url=url, version_identifier=edition, filename=_basename(url),
                      published_date=edition)


def _find_static_page_latest(source_name: str, spec: StaticPageLatestFind) -> _FindFacts:
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
    return _FindFacts(url=url, version_identifier=_token(best_href) or None,
                      filename=_basename(url))


def _find_json_manifest(source_name: str, spec: JsonManifestFind) -> _FindFacts:
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
    return _FindFacts(url=node, filename=_basename(node))


_FIND_DETERMINISTIC = {
    "direct_url": _find_direct_url,
    "dkan_catalog": _find_dkan_catalog,
    "static_page_latest": _find_static_page_latest,
    "json_manifest": _find_json_manifest,
}


# --- acquire: response straight to the store, OR whole into memory then store. #
#     A switch (stream). NEVER a temp disk file. Never reads the content. ----- #
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
            if stream:
                for chunk in resp.iter_content(chunk_size=buffer_size):
                    if not chunk:
                        continue
                    dst.write(chunk)
                    hasher.update(chunk)
                    size += len(chunk)
            else:
                data = resp.content
                dst.write(data)
                hasher.update(data)
                size = len(data)
        _log.LogPipeline("INFO", "data_fetch_agent[%s]: file %s (%d bytes) stream=%s",
                         source_name, store.path, size, stream)
        return hasher.hexdigest(), size, {
            "store_mode": "file", "path": store.path, "size_bytes": size}

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
    if stream:
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
    else:
        data = resp.content
        hasher.update(data)
        size = len(data)
        bc.upload_blob(data, overwrite=True)
    _log.LogPipeline("INFO", "data_fetch_agent[%s]: blob %s/%s (%d bytes) stream=%s",
                     source_name, store.container, store.blob_path, size, stream)
    return hasher.hexdigest(), size, {
        "store_mode": "blob", "container": store.container,
        "blob_path": store.blob_path, "size_bytes": size}


# --------------------------------------------------------------------------- #
# The agent: input FetchRequest, output FetchResult, one tool, model every run. #
# --------------------------------------------------------------------------- #
class _Deps(BaseModel):
    """Typed pydantic input the agent runs against."""
    request: FetchRequest
    http_timeout: int
    download: bool                        # False for a discover-only run
    headers: dict | None = None


def _resolve_discovered(req: FetchRequest, discovered_url: str | None) -> _FindFacts:
    if not discovered_url or not str(discovered_url).strip():
        raise ChatHealthyException(
            mode="discovery_no_matching_file", component="data_fetch_agent",
            message=(f"data_fetch_agent[{req.source_name}]: llm_discovery produced "
                     f"no URL for {getattr(req.find, 'page_url', '')}"))
    clean = str(discovered_url).strip().strip("`<>\"' \t\n")
    if not clean.startswith("http"):
        clean = urljoin(getattr(req.find, "page_url", ""), clean)
    if not _is_valid_url(clean):
        raise ChatHealthyException(
            mode="discovery_bad_url", component="data_fetch_agent",
            message=(f"data_fetch_agent[{req.source_name}]: unusable URL from "
                     f"discovery: {clean!r}"))
    return _FindFacts(url=clean, filename=_basename(clean))


def _execute_fetch(req: FetchRequest, *, discovered_url: str | None, download: bool,
                   headers: dict | None, http_timeout: int) -> FetchResult:
    """Resolve the source's download URL -- deterministically for every find
    mode except llm_discovery, where discovered_url is the URL the model read
    from the index page -- then download and store the bytes and return the
    FetchResult. The bytes never reach the model. This is the single fetch
    implementation shared by the deterministic (no-model) path and the agent
    tool path, so neither duplicates the other."""
    if req.find.mode == "llm_discovery":
        facts = _resolve_discovered(req, discovered_url)
    else:
        finder = _FIND_DETERMINISTIC.get(req.find.mode)
        if finder is None:
            raise ChatHealthyException(
                mode="runtime_error", component="data_fetch_agent",
                message=(f"data_fetch_agent[{req.source_name}]: unknown find mode "
                         f"{req.find.mode!r}"))
        facts = finder(req.source_name, req.find)
    if not download:
        return FetchResult(source_name=req.source_name, url=facts.url,
                           version_identifier=facts.version_identifier,
                           filename=facts.filename, published_date=facts.published_date,
                           sha256="", size_bytes=0, stored={"store_mode": "none"})
    buffer_size = req.buffer_size or (1024 * 1024)
    sha256, size, stored = _acquire(
        facts.url, source_name=req.source_name, store=req.store, stream=req.stream,
        buffer_size=buffer_size, headers=headers, http_timeout=http_timeout)
    return FetchResult(source_name=req.source_name, url=facts.url,
                       version_identifier=facts.version_identifier, filename=facts.filename,
                       published_date=facts.published_date, sha256=sha256,
                       size_bytes=size, stored=stored)


def fetch_source(ctx: RunContext[_Deps], discovered_url: str | None = None) -> FetchResult:
    """The agent's one tool and its output (the llm_discovery path). Delegates
    to _execute_fetch so the deterministic path and the model path run the same
    resolve/download/store logic."""
    return _execute_fetch(ctx.deps.request, discovered_url=discovered_url,
                          download=ctx.deps.download, headers=ctx.deps.headers,
                          http_timeout=ctx.deps.http_timeout)


_INSTRUCTIONS = (
    "You are the ChatHealthy data-fetch agent. You fetch exactly one source per "
    "run and you never read the file's contents. Produce your result by calling "
    "fetch_source exactly once. If the request's find mode is llm_discovery, "
    "first read the index page HTML given in your prompt, locate the single "
    "correct download URL the instructions describe, and call fetch_source with "
    "discovered_url set to exactly that URL. For every other find mode, call "
    "fetch_source with no arguments -- it resolves the URL itself. The source "
    "request is in your run dependencies."
)

_AGENT = None


def _model_name() -> str:
    model = os.getenv("CH_URL_DISCOVERY_MODEL", "").strip()
    if not model:
        raise ChatHealthyException(
            mode="config_error", component="data_fetch_agent",
            message=("data_fetch_agent: CH_URL_DISCOVERY_MODEL is not set "
                     "(expected a 'provider:model' string); there is no fallback."))
    return model


def _agent():
    """The one pydantic-ai Agent, built once per process. Model from the env; no
    vendor pinned in source (BUG-003)."""
    global _AGENT
    if _AGENT is None:
        _AGENT = Agent(_model_name(), name="data_fetch_agent", deps_type=_Deps,
                       output_type=fetch_source, instructions=_INSTRUCTIONS,
                       retries=3)
    return _AGENT


def _fetch_page_html(source_name: str, page_url: str, timeout_sec: int) -> str:
    try:
        resp = requests.get(page_url, timeout=timeout_sec, headers={"User-Agent": _UA})
        resp.raise_for_status()
    except Exception as exc:
        raise ChatHealthyException(
            mode="discovery_page_fetch_failed", component="data_fetch_agent",
            message=(f"data_fetch_agent[{source_name}]: cannot fetch index page "
                     f"{page_url}: {exc}"), exception=exc) from exc
    html = resp.text[:_PAGE_HTML_CHARS]
    if not html.strip():
        raise ChatHealthyException(
            mode="discovery_page_empty", component="data_fetch_agent",
            message=(f"data_fetch_agent[{source_name}]: index page {page_url} "
                     f"returned empty body"))
    return html


def _prompt(request: FetchRequest) -> str:
    lines = [f"Fetch source {request.source_name!r}. find mode: {request.find.mode}."]
    if request.find.mode == "llm_discovery":
        html = _fetch_page_html(request.source_name, request.find.page_url, 60)
        lines += [f"instructions: {request.find.instructions or '(none)'}",
                  f"page_url: {request.find.page_url}",
                  "The index page HTML is between the markers; find the one download "
                  "URL and pass it as discovered_url.",
                  "<<<PAGE_HTML>>>", html, "<<<END_PAGE_HTML>>>"]
    else:
        lines.append("Call fetch_source with no arguments.")
    return "\n".join(lines)


def _run(request: FetchRequest, *, download: bool, headers: dict | None,
         http_timeout: int) -> FetchResult:
    # Deterministic find modes resolve + download WITHOUT a model: no
    # pydantic-ai Agent is constructed and CH_URL_DISCOVERY_MODEL is never
    # read. Only llm_discovery drives the model. The whole point is that a
    # prefabricated, deterministic fetch_spec needs no model at all.
    if request.find.mode != "llm_discovery":
        return _execute_fetch(request, discovered_url=None, download=download,
                              headers=headers, http_timeout=http_timeout)
    deps = _Deps(request=request, headers=headers, http_timeout=http_timeout,
                 download=download)
    result = _agent().run_sync(_prompt(request), deps=deps)
    out = getattr(result, "output", None)
    if not isinstance(out, FetchResult):
        raise ChatHealthyException(
            mode="data_fetch_incomplete", component="data_fetch_agent",
            message=(f"data_fetch_agent[{request.source_name}]: the agent did not "
                     f"return a FetchResult"))
    return out


# --------------------------------------------------------------------------- #
# Public entries.                                                             #
# --------------------------------------------------------------------------- #
def run(request: FetchRequest, *, headers: dict | None = None,
        http_timeout: int = _DEFAULT_TIMEOUT) -> FetchResult:
    """Run the agent end to end: find the URL, download, store. Streamed
    chunk-by-chunk when request.stream, else swallowed whole into memory and
    written once. No temp disk."""
    if request.stream and not request.buffer_size:
        raise ChatHealthyException(
            mode="runtime_error", component="data_fetch_agent",
            message=(f"data_fetch_agent[{request.source_name}]: stream=True requires "
                     f"a buffer_size"))
    return _run(request, download=True, headers=headers, http_timeout=http_timeout)


def find_latest_source_version(*, source_name: str, page_url: str, instructions: str,
                               timeout_sec: int = 60) -> dict:
    """Discover-only: run the agent to locate the latest file's URL on an index
    page, without downloading. Used by callers that do their own download."""
    request = FetchRequest(
        source_name=source_name,
        find=LlmDiscoveryFind(page_url=page_url, instructions=instructions),
        store=FileStore(path=""), stream=False)
    out = _run(request, download=False, headers=None, http_timeout=timeout_sec)
    _log.LogPipeline("INFO", "data_fetch_agent[%s]: discovery resolved url=%s",
                     source_name, out.url)
    return {"url": out.url, "version_identifier": out.version_identifier,
            "filename": out.filename or _basename(out.url),
            "published_date": out.published_date}


def find_latest_data_url(*, source_name: str, page_url: str, instructions: str,
                         timeout_sec: int = 60) -> str:
    """URL-only shim over find_latest_source_version."""
    return find_latest_source_version(source_name=source_name, page_url=page_url,
                                      instructions=instructions, timeout_sec=timeout_sec)["url"]
