# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Prescriber Load Worker — builds provider_quality collection from providers.
# LEFT OUTER JOIN: every provider gets a quality record.
# CMS Part D data enriches where it exists. Providers without prescriber
# data still get exclusion checks, specialty match, experience.
#
# Uses PipelineWorkerBase for chunked processing.

import csv
import io
import logging
import os
from datetime import datetime, timezone

from pymongo import UpdateOne
from pipeline_worker_base import PipelineWorkerBase
from blob_client import get_blob_service
from pipeline_db import get_db

_log = logging.getLogger("prescriber_load")


def _primary_practice_address(provider: dict) -> dict:
    """Return the primary practice_address as a dict. Handles both shapes:
    list-of-addresses (post-multi-practice-address) and legacy single-dict."""
    pa = provider.get("practice_address")
    if isinstance(pa, list):
        return pa[0] if pa and isinstance(pa[0], dict) else {}
    if isinstance(pa, dict):
        return pa
    return {}


def _primary_county(provider: dict) -> dict:
    """Return the primary county sub-doc for the provider.
    Prefers per-element county on the primary practice address; falls back to
    the doc-level county field for legacy records."""
    addr = _primary_practice_address(provider)
    if isinstance(addr.get("county"), dict):
        return addr["county"]
    return provider.get("county") or {}


# CMS Part D CSV columns
COL_NPI = "Prscrbr_NPI"
COL_STATE = "Prscrbr_State_Abrvtn"
COL_BRAND = "Brnd_Name"
COL_GENERIC = "Gnrc_Name"
COL_CLAIMS = "Tot_Clms"
COL_BENEFICIARIES = "Tot_Benes"
COL_30DAY = "Tot_30day_Fills"
COL_DAYS_SUPPLY = "Tot_Day_Suply"
COL_COST = "Tot_Drug_Cst"


class PrescriberLoadWorker(PipelineWorkerBase):
    """Build provider_quality from providers collection, enrich with CMS Part D."""

    def __init__(self, config: dict):
        super().__init__(config)
        self.env_prefix = config.get("env_prefix", "dev")
        self.states = config.get("states", ["DE"])
        self.blob_name = config.get("blob_name", "cms_partd_prescriber_latest.csv")
        self.container = config.get("blob_container", "provider-data")
        self.batch_size = config.get("batch_size", 500)

        self._cms_by_npi = {}  # NPI → list of drug rows from CMS
        self._provider_cursor = None
        self._current_provider = None
        self._rows_processed = 0
        self._npis_loaded = 0
        self._npis_with_rx = 0
        self._batch = []           # provider_quality writes
        self._provider_batch = []  # providers.can_prescribe.drugs writes

    def _quality_collection(self):
        return get_db(self.env_prefix)["provider_quality"]

    def _provider_collection(self):
        return get_db(self.env_prefix)["providers"]

    # ── PipelineWorkerBase contract ────────────────────────────────────────

    def _pipeline_open(self):
        """Load CMS Part D data into memory keyed by NPI, then open provider cursor."""

        # Step A: Load CMS Part D CSV, filter by state, group by NPI
        _log.info("Loading CMS Part D from blob: %s/%s", self.container, self.blob_name)
        _log.info("State filter: %s", self.states)

        blob_service = get_blob_service()
        blob_client = blob_service.get_blob_client(self.container, self.blob_name)
        stream = blob_client.download_blob()

        # v4-001D: Stream line-by-line — O(1) memory, never hold full CSV
        _log.info("Streaming CMS Part D CSV (line-by-line)...")

        def _line_iterator(blob_stream):
            """Yield decoded lines from blob chunks without assembling full content."""
            buf = ""
            total_bytes = 0
            for chunk in blob_stream.chunks():
                total_bytes += len(chunk)
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    yield line
                if total_bytes % (500 * 1024 * 1024) < 65536:
                    _log.info("  Streamed %d MB...", total_bytes // (1024 * 1024))
            if buf.strip():
                yield buf

        reader = csv.DictReader(_line_iterator(stream))
        state_set = set(self.states)
        skipped = 0

        for row in reader:
            state = (row.get(COL_STATE) or "").strip().upper()
            if state not in state_set:
                skipped += 1
                continue

            npi = (row.get(COL_NPI) or "").strip()
            if not npi:
                continue

            drug = {
                "brand_name": (row.get(COL_BRAND) or "").strip(),
                "generic_name": (row.get(COL_GENERIC) or "").strip(),
                "total_claims": _safe_int(row.get(COL_CLAIMS)),
                "total_beneficiaries": _safe_int(row.get(COL_BENEFICIARIES)),
                "total_30day_fills": _safe_int(row.get(COL_30DAY)),
                "total_days_supply": _safe_int(row.get(COL_DAYS_SUPPLY)),
                "total_drug_cost": _safe_float(row.get(COL_COST)),
            }

            if npi not in self._cms_by_npi:
                self._cms_by_npi[npi] = []
            self._cms_by_npi[npi].append(drug)

        _log.info("CMS data: %d NPIs with drug data, %d rows skipped (wrong state)",
                  len(self._cms_by_npi), skipped)

        # Step B: Open cursor on ALL providers in our states
        state_filter = {"practice_address.state": {"$in": self.states}}
        total_providers = self._provider_collection().count_documents(state_filter)
        _log.info("Providers in %s: %d — building provider_quality for ALL", self.states, total_providers)

        self._provider_cursor = self._provider_collection().find(
            state_filter,
            {"npi": 1, "practice_address": 1, "county": 1,
             "taxonomy_codes": 1, "enumeration_date": 1, "_id": 0}
        )

    def _pipeline_has_next(self):
        try:
            self._current_provider = next(self._provider_cursor)
            return True
        except StopIteration:
            if self._batch:
                self._flush_batch()
            return False

    def _pipeline_row_key(self):
        return self._current_provider.get("npi", "unknown")

    def _pipeline_process(self):
        """Build provider_quality document — left outer join with CMS data.

        Dual-write:
          1. provider_quality — full scoring record for EvaluateCare
          2. providers.can_prescribe.drugs — drug list for FindCare filter + embedding
        """
        provider = self._current_provider
        npi = provider.get("npi", "")
        if not npi:
            return

        addr = _primary_practice_address(provider)
        county = _primary_county(provider)

        # Location — always populated from provider record
        location = {
            "state": addr.get("state", ""),
            "city": addr.get("city", ""),
            "zip": addr.get("zip", ""),
            "county": county.get("name", ""),
        }

        # Specialty match — always populated from NPI taxonomy
        specialty_match = {
            "taxonomy_codes": provider.get("taxonomy_codes", []),
        }

        # Experience — always populated from NPI enumeration date
        experience = {
            "enumeration_date": provider.get("enumeration_date", ""),
        }

        # Prescriber behavior — populated only if CMS data exists (LEFT OUTER JOIN)
        cms_drugs = self._cms_by_npi.get(npi)
        generic_ratio_band = None
        if cms_drugs:
            drug_list, generic_ratio_band = _group_drugs(cms_drugs)
            prescriber_behavior = {
                "drugs": drug_list,
                "total_unique_drugs": len(drug_list),
                "cost_measures": {
                    "generic_ratio_band": generic_ratio_band,
                },
                "data_year": 2023,
                "source": "CMS Part D Prescriber PUF RY25",
                "loaded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._npis_with_rx += 1
        else:
            drug_list = []
            prescriber_behavior = {}

        # Count how many measures have data
        scoreable = 0
        if prescriber_behavior:
            scoreable += 1
        if specialty_match.get("taxonomy_codes"):
            scoreable += 1
        if experience.get("enumeration_date"):
            scoreable += 1
        # exclusion and board_cert counted after enrichment

        # Write 1: provider_quality (EvaluateCare scoring)
        self._batch.append(UpdateOne(
            {"npi": npi},
            {"$set": {
                "npi": npi,
                "location": location,
                "measures": {
                    "prescriber_behavior": prescriber_behavior,
                    "exclusion": {"oig_excluded": None, "sam_excluded": None},
                    "specialty_match": specialty_match,
                    "experience": experience,
                    "board_cert": {},
                },
                "scoreable_measures": scoreable,
                "total_measures": 5,
                "loaded_at": datetime.now(timezone.utc).isoformat(),
            }},
            upsert=True,
        ))

        # Write 2: providers.can_prescribe.drugs (FindCare filter + embedding)
        # Only write drugs if CMS data exists for this NPI
        if drug_list:
            provider_update = {
                "can_prescribe.drugs": drug_list,
                "can_prescribe.cost_measures.generic_ratio_band": generic_ratio_band,
                "can_prescribe.data_year": 2023,
                "can_prescribe.data_source": "CMS Part D Prescriber PUF RY25",
                "can_prescribe.drugs_loaded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._provider_batch.append(UpdateOne(
                {"npi": npi},
                {"$set": provider_update},
            ))

        self._rows_processed += 1

        if len(self._batch) >= self.batch_size:
            self._flush_batch()

    def _flush_batch(self):
        if self._batch:
            self._quality_collection().bulk_write(self._batch, ordered=False)
            self._npis_loaded += len(self._batch)
            _log.info("Batch: %d NPIs → provider_quality (total: %d, with Rx: %d)",
                      len(self._batch), self._npis_loaded, self._npis_with_rx)
            self._batch = []

        # Dual-write: update providers.can_prescribe.drugs for FindCare
        if self._provider_batch:
            self._provider_collection().bulk_write(self._provider_batch, ordered=False)
            _log.info("  → %d NPIs updated in providers.can_prescribe.drugs",
                      len(self._provider_batch))
            self._provider_batch = []

    def _pipeline_resume(self):
        pass

    def _pipeline_close(self):
        if self._provider_cursor:
            self._provider_cursor.close()
        _log.info("Load complete: %d providers → provider_quality (%d with prescriber data)",
                  self._npis_loaded, self._npis_with_rx)

    def _pipeline_build_result(self):
        return {
            "status": "complete",
            "npis_loaded": self._npis_loaded,
            "npis_with_prescriber_data": self._npis_with_rx,
            "states": self.states,
        }


# ── Helpers ────────────────────────────────────────────────────────────────

def _is_generic(brand_name: str, generic_name: str) -> bool:
    """Determine if a CMS drug row represents a generic prescription.

    In CMS Part D data, each row has a brand_name and generic_name.
    When the brand name closely matches the generic name (case-insensitive,
    ignoring suffixes like 'Calcium', 'HCl', etc.), it's a generic product.
    When the brand name is a distinct trade name (e.g., 'Lipitor' vs
    'Atorvastatin'), it's a brand product.
    """
    b = brand_name.strip().upper()
    g = generic_name.strip().upper()
    if not b or not g:
        return True  # default to generic if unknown
    # Exact match or brand starts with generic name → generic product
    return b == g or b.startswith(g) or g.startswith(b)


def _compute_band(pct: float) -> str:
    """Convert a percentage (0-100) to a 5% band string.

    Returns one of 20 bands: '0-5%', '5-10%', ... '95-100%'.
    """
    lower = int(pct // 5) * 5
    lower = min(lower, 95)  # cap at 95-100%
    upper = lower + 5
    return f"{lower}-{upper}%"


def _group_drugs(raw_drugs):
    """Group raw CMS drug rows by molecule (generic name), aggregate claims.

    Splits claims into brand_claims and generic_claims per molecule
    (EPIC-006-F-010-S-004-REQ-T-010). Also computes provider-level generic_ratio_band
    (EPIC-006-F-010-S-004-REQ-T-011).
    """
    molecule_map = {}
    for d in raw_drugs:
        mol = d["generic_name"] or d["brand_name"]
        if mol not in molecule_map:
            molecule_map[mol] = {
                "molecule": mol,
                "brand_names": set(),
                "brand_claims": 0,
                "generic_claims": 0,
                "total_claims": 0,
                "total_beneficiaries": 0,
            }
        entry = molecule_map[mol]
        if d["brand_name"]:
            entry["brand_names"].add(d["brand_name"])

        claims = d["total_claims"] or 0
        if _is_generic(d["brand_name"], d["generic_name"]):
            entry["generic_claims"] += claims
        else:
            entry["brand_claims"] += claims
        entry["total_claims"] += claims
        entry["total_beneficiaries"] += d["total_beneficiaries"] or 0

    drug_list = []
    total_brand = 0
    total_generic = 0

    for mol, entry in molecule_map.items():
        drug_list.append({
            "molecule": entry["molecule"],
            "brand_names": sorted(entry["brand_names"]),
            "brand_claims": entry["brand_claims"],
            "generic_claims": entry["generic_claims"],
            "total_claims": entry["total_claims"],
            "total_beneficiaries": entry["total_beneficiaries"],
            "indications": [],
            "icd10_codes": [],
        })
        total_brand += entry["brand_claims"]
        total_generic += entry["generic_claims"]

    drug_list.sort(key=lambda d: d["total_claims"], reverse=True)

    # Provider-level generic ratio band (20 bands, 5% each)
    total_all = total_brand + total_generic
    generic_pct = (total_generic / total_all * 100) if total_all > 0 else 0
    generic_ratio_band = _compute_band(generic_pct)

    return drug_list, generic_ratio_band


def _safe_int(val):
    try:
        return int(val)
    except (ValueError, TypeError):
        return 0


def _safe_float(val):
    try:
        return float(val)
    except (ValueError, TypeError):
        return 0.0
