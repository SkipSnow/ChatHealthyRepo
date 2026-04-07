# Copyright (c) 2026 Skip Snow. All rights reserved.
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

from pymongo import MongoClient, UpdateOne
from pipeline_worker_base import PipelineWorkerBase
from blob_client import get_blob_service

_log = logging.getLogger("prescriber_load")

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

        self._mongo = None
        self._cms_by_npi = {}  # NPI → list of drug rows from CMS
        self._provider_cursor = None
        self._current_provider = None
        self._rows_processed = 0
        self._npis_loaded = 0
        self._npis_with_rx = 0
        self._batch = []

    def _get_db(self):
        if self._mongo is None:
            self._mongo = MongoClient(os.environ["MONGO_connectionString"])
        return self._mongo

    def _quality_collection(self):
        db = self._get_db()
        return db[f"{self.env_prefix}_PublicHealthData"]["provider_quality"]

    def _provider_collection(self):
        db = self._get_db()
        return db[f"{self.env_prefix}_PublicHealthData"]["providers"]

    # ── PipelineWorkerBase contract ────────────────────────────────────────

    def _pipeline_open(self):
        """Load CMS Part D data into memory keyed by NPI, then open provider cursor."""

        # Step A: Load CMS Part D CSV, filter by state, group by NPI
        _log.info("Loading CMS Part D from blob: %s/%s", self.container, self.blob_name)
        _log.info("State filter: %s", self.states)

        blob_service = get_blob_service()
        blob_client = blob_service.get_blob_client(self.container, self.blob_name)
        stream = blob_client.download_blob()
        content = stream.readall().decode("utf-8", errors="replace")

        _log.info("CSV loaded (%d bytes). Parsing and filtering...", len(content))

        reader = csv.DictReader(io.StringIO(content))
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
        """Build provider_quality document — left outer join with CMS data."""
        provider = self._current_provider
        npi = provider.get("npi", "")
        if not npi:
            return

        addr = provider.get("practice_address", {})
        county = provider.get("county", {})

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
        if cms_drugs:
            drug_list = _group_drugs(cms_drugs)
            prescriber_behavior = {
                "drugs": drug_list,
                "total_unique_drugs": len(drug_list),
                "data_year": 2023,
                "source": "CMS Part D Prescriber PUF RY25",
                "loaded_at": datetime.now(timezone.utc).isoformat(),
            }
            self._npis_with_rx += 1
        else:
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

        self._rows_processed += 1

        if len(self._batch) >= self.batch_size:
            self._flush_batch()

    def _flush_batch(self):
        if not self._batch:
            return
        coll = self._quality_collection()
        result = coll.bulk_write(self._batch, ordered=False)
        self._npis_loaded += len(self._batch)
        _log.info("Batch: %d NPIs (total: %d, with Rx: %d)",
                  len(self._batch), self._npis_loaded, self._npis_with_rx)
        self._batch = []

    def _pipeline_resume(self):
        pass

    def _pipeline_close(self):
        if self._provider_cursor:
            self._provider_cursor.close()
        _log.info("Load complete: %d providers → provider_quality (%d with prescriber data)",
                  self._npis_loaded, self._npis_with_rx)

    def _pipeline_result(self):
        return {
            "status": "complete",
            "npis_loaded": self._npis_loaded,
            "npis_with_prescriber_data": self._npis_with_rx,
            "states": self.states,
        }


# ── Helpers ────────────────────────────────────────────────────────────────

def _group_drugs(raw_drugs):
    """Group raw CMS drug rows by molecule (generic name), aggregate claims."""
    molecule_map = {}
    for d in raw_drugs:
        mol = d["generic_name"] or d["brand_name"]
        if mol not in molecule_map:
            molecule_map[mol] = {
                "molecule": mol,
                "brand_names": set(),
                "generic_names": set(),
                "total_claims": 0,
                "total_beneficiaries": 0,
            }
        entry = molecule_map[mol]
        if d["brand_name"]:
            entry["brand_names"].add(d["brand_name"])
        if d["generic_name"]:
            entry["generic_names"].add(d["generic_name"])
        entry["total_claims"] += d["total_claims"] or 0
        entry["total_beneficiaries"] += d["total_beneficiaries"] or 0

    drug_list = []
    for mol, entry in molecule_map.items():
        drug_list.append({
            "molecule": entry["molecule"],
            "brand_names": sorted(entry["brand_names"]),
            "generic_names": sorted(entry["generic_names"]),
            "total_claims": entry["total_claims"],
            "total_beneficiaries": entry["total_beneficiaries"],
            "indications": [],
            "icd10_codes": [],
        })

    drug_list.sort(key=lambda d: d["total_claims"], reverse=True)
    return drug_list


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
