"""Single-source-of-truth loader for the proprietary NUCC classification catalog.

Maps every NUCC taxonomy code to booleans:
  - can_prescribe     (true if the role has prescribing authority)
  - is_homeopathic    (true if the role is homeopathic)
  - is_npi_registered (optional; defaults False)
  - is_disqualified   (optional; defaults False. Curated per BUG-005.)

The catalog JSON is a public document published to the ChatHealthy website
under Data/. The URL is passed to this process as the env var
SPECIALTY_CLASSIFICATION_CATALOG_URL via the standard deploy chain
(declared in deployment_architecture.json, sourced from .env, pushed
to KV + Automation Variable, hydrated into the Controller container).

No fallbacks. Missing env, HTTP error, or empty result raises.
"""
from __future__ import annotations
from chathealthy_lib.exceptions import ChatHealthyException

import json
import os
import ssl
import threading
import urllib.error
import urllib.request
from typing import Dict

try:
    import certifi as _certifi
    _CA_BUNDLE = _certifi.where()
except ImportError:
    _CA_BUNDLE = None


_CATALOG_URL_ENV = "SPECIALTY_CLASSIFICATION_CATALOG_URL"

_cache: Dict[str, Dict[str, bool]] | None = None
_cache_lock = threading.Lock()


def load_catalog() -> Dict[str, Dict[str, bool]]:
    """Return {nucc_code: {"can_prescribe": bool, "is_homeopathic": bool, ...}}."""
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        url = os.environ.get(_CATALOG_URL_ENV, "").strip()
        if not url:
            raise ChatHealthyException(
                mode="runtime_error",
                message=f"{_CATALOG_URL_ENV} env var is not set",
                component="specialty_classification_catalog",
            )
        ctx = ssl.create_default_context(cafile=_CA_BUNDLE) if _CA_BUNDLE else ssl.create_default_context()
        auth_header = os.environ.get("CLOUDFLARE_PIPELINE_AUTH_HEADER", "").strip()
        if not auth_header:
            raise ChatHealthyException(
                mode="runtime_error",
                message=(
                    "CLOUDFLARE_PIPELINE_AUTH_HEADER env var is not set. "
                    "This secret is the value Cloudflare's firewall rule "
                    "checks to admit pipeline requests to chathealthy.ai/Data/."
                ),
                component="specialty_classification_catalog",
            )
        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "ChatHealthy-Pipeline/1.0",
                "X-ChatHealthy-Pipeline-Auth": auth_header,
            },
        )
        with urllib.request.urlopen(req, context=ctx, timeout=30) as r:
            doc = json.loads(r.read().decode("utf-8"))
        rows = doc.get("classifications") if isinstance(doc, dict) else doc
        if not isinstance(rows, list):
            raise ChatHealthyException(
                mode="runtime_error",
                message=f"specialty_classification_catalog at {url} did not contain a 'classifications' list",
                component="specialty_classification_catalog",
                url=url,
            )
        out: Dict[str, Dict[str, object]] = {}
        for row in rows:
            code = row.get("code")
            if not code:
                continue
            homeopathic = row.get("is_homeopathic")
            if homeopathic is None:
                homeopathic = row.get("homeopathic", False)
            # is_supplemented is a nested object {value: bool, note?: str} per
            # the F-105 schema. Extract the boolean value; when true, the entry
            # also carries the NUCC-equivalent description peers per
            # NUCC_SpecialtyCodeDataDiscrepancyManagement.docx.
            is_supp = row.get("is_supplemented")
            if isinstance(is_supp, dict):
                is_supplemented = bool(is_supp.get("value"))
            elif isinstance(is_supp, bool):
                is_supplemented = is_supp
            else:
                is_supplemented = False
            out[code] = {
                "can_prescribe": bool(row.get("can_prescribe", False)),
                "is_homeopathic": bool(homeopathic),
                "is_supplemented": is_supplemented,
                "grouping": row.get("grouping"),
                "classification": row.get("classification"),
                "specialization": row.get("specialization"),
                "display_name": row.get("display_name"),
                "definition": row.get("definition"),
                "is_npi_registered": bool(row.get("is_npi_registered", False)),
                "is_disqualified": bool(row.get("is_disqualified", False)),
            }
        if not out:
            raise ChatHealthyException(
                mode="runtime_error",
                message=f"specialty_classification_catalog at {url} returned 0 entries",
                component="specialty_classification_catalog",
                url=url,
            )
        _cache = out
        return _cache


def clear_cache() -> None:
    """Test-only: force the next load_catalog() to re-read."""
    global _cache
    with _cache_lock:
        _cache = None
