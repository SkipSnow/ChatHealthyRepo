# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Prescriber Data Fetcher — downloads CMS Part D, OIG LEIE, and SAM.gov files.
# Uses DataFetcherBase ETag guard — skips download if file unchanged.

import logging
import os

import requests as _requests

from data_fetcher_base import DataFetcherBase

_log = logging.getLogger("prescriber_fetcher")

# ── CMS Part D Prescriber by Provider and Drug ─────────────────────────────

# Stable dataset UUID — identifies the dataset, not a specific version.
# The dataset-resources API always returns the latest file as the first entry.
CMS_PARTD_DATASET_UUID = "9552739e-3d05-4c1b-8eff-ecabf391e2e5"
CMS_PARTD_RESOURCES_URL = f"https://data.cms.gov/data-api/v1/dataset-resources/{CMS_PARTD_DATASET_UUID}"

# Fallback URL — used only if the dataset-resources API is unreachable.
CMS_PARTD_FALLBACK_URL = (
    "https://data.cms.gov/sites/default/files/2025-04/"
    "0d5915ce-002c-4d87-bde8-24ffb08bb6cc/"
    "MUP_DPR_RY25_P04_V10_DY23_NPIBN.csv"
)


def _discover_cms_partd_url() -> str:
    """Discover the latest CMS Part D Prescriber CSV download URL via API."""
    try:
        resp = _requests.get(CMS_PARTD_RESOURCES_URL, timeout=15)
        resp.raise_for_status()
        data = resp.json().get("data", [])
        for entry in data:
            if entry.get("downloadURL", "").endswith(".csv"):
                url = entry["downloadURL"]
                _log.info("Discovered CMS Part D URL: %s", url)
                return url
        _log.warning("No CSV found in dataset-resources response, using fallback")
        return CMS_PARTD_FALLBACK_URL
    except Exception as e:
        _log.warning("CMS dataset-resources API failed: %s — using fallback URL", e)
        return CMS_PARTD_FALLBACK_URL


class CMSPartDFetcher(DataFetcherBase):
    source_name = "cms_partd_prescriber"

    @property
    def source_url(self):
        return _discover_cms_partd_url()

    def blob_name(self):
        return "cms_partd_prescriber_latest.csv"


# ── OIG LEIE Exclusion List ────────────────────────────────────────────────

OIG_LEIE_URL = "https://oig.hhs.gov/exclusions/downloadables/UPDATED.csv"

class OIGLEIEFetcher(DataFetcherBase):
    source_name = "oig_leie"
    source_url = OIG_LEIE_URL

    def blob_name(self):
        return "oig_leie_latest.csv"


# ── SAM.gov Exclusion List ─────────────────────────────────────────────────
# SAM.gov requires API key for bulk download. Public CSV endpoint:
SAM_EXCLUSION_URL = "https://sam.gov/api/prod/fileextractservices/v2/api/download/exclusions/csv"

class SAMExclusionFetcher(DataFetcherBase):
    source_name = "sam_exclusion"
    source_url = SAM_EXCLUSION_URL

    def blob_name(self):
        return "sam_exclusion_latest.csv"


# ── Convenience: fetch all ─────────────────────────────────────────────────

def fetch_all(config: dict = None) -> dict:
    """Download all three source files. Returns dict of results."""
    config = config or {}
    results = {}

    for name, cls in [
        ("cms_partd", CMSPartDFetcher),
        ("oig_leie", OIGLEIEFetcher),
        ("sam_exclusion", SAMExclusionFetcher),
    ]:
        _log.info("Fetching %s...", name)
        try:
            fetcher = cls(config)
            results[name] = fetcher.fetch()
            _log.info("  %s: %s", name, results[name].get("status", "done"))
        except Exception as e:
            _log.error("  %s FAILED: %s", name, e)
            results[name] = {"status": "error", "error": str(e)}

    return results
