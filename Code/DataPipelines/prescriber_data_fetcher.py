# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Prescriber Data Fetcher — downloads CMS Part D, OIG LEIE, and SAM.gov files.
# Uses DataFetcherBase ETag guard — skips download if file unchanged.

import logging
import os

from data_fetcher_base import DataFetcherBase

_log = logging.getLogger("prescriber_fetcher")

# ── CMS Part D Prescriber by Provider and Drug ─────────────────────────────

CMS_PARTD_URL = (
    "https://data.cms.gov/sites/default/files/2025-04/"
    "0d5915ce-002c-4d87-bde8-24ffb08bb6cc/"
    "MUP_DPR_RY25_P04_V10_DY23_NPIBN.csv"
)

class CMSPartDFetcher(DataFetcherBase):
    source_name = "cms_partd_prescriber"
    source_url = CMS_PARTD_URL

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
