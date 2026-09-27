# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""source_freshness_probe — per-source version-identifier probes.

Per Skip 2026-07-29: every pipeline run MUST contact the source and read
the version the source itself publishes (Last-Modified / ETag / cycle
identifier), compare it to the identifier stored in
`admin.DataSourceRegistry.version`, and only re-download when the source
has changed. The design explicitly says sources sometimes update
out-of-band, so a TTL-based cache would miss those updates.

Each probe returns a stable, comparable string:

    probe_source_version(source_name, url, auth_headers=None) -> str

The returned string is opaque to freshness_gate — it just compares it
character-for-character against DataSourceRegistry.version. What the
string contains is per-source: an HTTP ETag, a Last-Modified stamp, a
filename, a JSON hash — whatever the source publishes as its own
version signal.

Probe mechanism today: HTTP HEAD on the current source URL, reading
ETag and Last-Modified headers. If neither header is present the probe
falls back to Content-Length (weak signal but non-zero) so we still get
a comparable identifier.

For sources whose URL itself rotates monthly (e.g., NPPES monthly
dissemination), use source_url_discovery.find_latest_source_version
instead — its structured LLM output includes a version_identifier field
the freshness_gate can compare exactly like this probe's return value.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

import os
from typing import Optional

import requests

_log = ChatHealthyLoggingService()

_PROBE_TIMEOUT_SECONDS = 30
_UA = "ChatHealthy-Pipeline/1.0"


def probe_source_version(
    source_name: str,
    url: str,
    auth_headers: Optional[dict] = None,
) -> str:
    """HEAD the source URL and return a version identifier composed from
    the headers the origin publishes. Prefer ETag; fall back to
    Last-Modified; last resort Content-Length. The composite string is
    the source-side version-of-record for this URL at this instant.

    auth_headers: any extra headers required to pass origin gates (e.g.,
    Cloudflare's X-ChatHealthy-Pipeline-Auth for chathealthy.ai/Data/*).
    """
    headers = {"User-Agent": _UA}
    if auth_headers:
        headers.update(auth_headers)
    auth = os.environ.get("CLOUDFLARE_PIPELINE_AUTH_HEADER", "").strip()
    if auth and "chathealthy.ai" in url and "X-ChatHealthy-Pipeline-Auth" not in headers:
        headers["X-ChatHealthy-Pipeline-Auth"] = auth
    try:
        resp = requests.head(
            url, headers=headers, timeout=_PROBE_TIMEOUT_SECONDS,
            allow_redirects=True,
        )
    except Exception as exc:
        raise ChatHealthyException(
            mode="runtime_error",
            message=(
                f"source_freshness_probe[{source_name}]: HEAD {url} "
                f"failed: {type(exc).__name__}: {exc}"
            ),
            component="source_freshness_probe",
            source_name=source_name,
            exception=exc) from exc
    if resp.status_code >= 400:
        raise ChatHealthyException(
            mode="runtime_error",
            message=(
                f"source_freshness_probe[{source_name}]: HEAD {url} "
                f"returned HTTP {resp.status_code}"
            ),
            component="source_freshness_probe",
            source_name=source_name,
            http_status=resp.status_code,
        )
    etag = (resp.headers.get("ETag") or "").strip().strip('"')
    lastmod = (resp.headers.get("Last-Modified") or "").strip()
    clen = (resp.headers.get("Content-Length") or "").strip()
    parts = []
    if etag:
        parts.append(f"etag={etag}")
    if lastmod:
        parts.append(f"lastmod={lastmod}")
    if clen:
        parts.append(f"clen={clen}")
    if not parts:
        raise ChatHealthyException(
            mode="runtime_error",
            message=(
                f"source_freshness_probe[{source_name}]: HEAD {url} "
                f"returned no ETag, Last-Modified, or Content-Length — "
                f"cannot derive a version identifier"
            ),
            component="source_freshness_probe",
            source_name=source_name,
        )
    return ";".join(parts)
