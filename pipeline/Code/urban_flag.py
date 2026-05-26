# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""USDA RUCC urban-flag stamper.

Realizes EPIC-010-F-103-S-005. Uses USDA Rural-Urban Continuum Codes (NOT
the Census 2020 Urban-Area / County relationship file). RUCC 1-3 → urban,
RUCC 4-9 → rural. Field name stamped on every in-scope
`practice_address[*].county` is `urban` (boolean).

URL discovery: AI-agent (F-102-S-003-REQ-T-006). No fallback constant.
"""

from __future__ import annotations

import io
import logging
import os

import requests

_log = logging.getLogger("urban_flag")


_USDA_RUCC_INDEX_URL = "https://www.ers.usda.gov/data-products/rural-urban-continuum-codes/"


def stamp_urban_flag_fn(config):
    stamper = UrbanFlagStamper(**config)
    stamper.run()
    return {"summary": stamper.stats}


class UrbanFlagStamper:
    """Stamp `addresses[*].county.urban` (bool) from USDA RUCC codes 1-9.

    1..3 → urban=True. 4..9 → urban=False.
    Every address entry (business AND practice) whose own `state` matches
    the run's `states` config gets stamped; out-of-state elements are
    left alone. Post-schema-reconciliation: practice_address + mailing_address
    are unified into addresses[] with address_type discriminator.
    """

    URBAN_RUCC_MAX = 3  # codes <= 3 are urban; 4..9 are rural

    def __init__(self, blob_container, states, provider_collection, bulk_batch_size):
        self.blob_container = blob_container
        self.states = states
        self.provider_collection = provider_collection
        self.bulk_batch_size = bulk_batch_size
        # county_fips (5-digit) → bool (True if RUCC 1..3, False if 4..9)
        self.rucc_urban: dict[str, bool] = {}
        self.stats = {
            "providers_scanned": 0,
            "elements_scanned": 0,
            "elements_stamped": 0,
            "elements_no_county_fips": 0,
            "elements_unmatched_rucc": 0,
            "rucc_loaded_counties": 0,
            "rucc_source_url": None,
            "state_exceptions": [],
            "provider_exceptions": [],
            "element_exceptions": [],
        }

    def run(self):
        self.load_data()
        self.write_data_to_collection()

    # ── Source ingest ────────────────────────────────────────────────────────
    def load_data(self):
        try:
            from blob_client import get_blob_service
            from openpyxl import load_workbook
            from source_url_discovery import find_latest_data_url

            # Agent-discover the latest RUCC XLSX URL.
            rucc_url = find_latest_data_url(
                source_name="usda_rucc",
                page_url=_USDA_RUCC_INDEX_URL,
                instructions=(
                    "Find the URL of the latest USDA Rural-Urban Continuum "
                    "Codes (RUCC) spreadsheet file. Rules: (a) Look for the "
                    "most recent year's RUCC file (filename typically "
                    "`Ruralurbancontinuumcodes<YEAR>.xlsx` or similar). "
                    "(b) MUST be an XLSX or CSV, not PDF. (c) Pick the file "
                    "that contains the actual county-level RUCC codes 1-9, "
                    "not documentation. (d) Return only the absolute URL."
                ),
            )
            self.stats["rucc_source_url"] = rucc_url

            # Cache in blob; if blob already has it, skip the HTTP fetch.
            # Blob name carries the URL's leaf so a new release lands as a new blob.
            blob_name = "usda_" + rucc_url.rsplit("/", 1)[-1].split("?")[0]
            blob = (
                get_blob_service()
                .get_container_client(self.blob_container)
                .get_blob_client(blob_name)
            )
            try:
                data = blob.download_blob().readall()
                _log.info("urban_flag: blob hit %s (%d bytes)", blob_name, len(data))
            except Exception as exc:
                _log.info("urban_flag: blob miss (%s) — fetching USDA", exc)
                resp = requests.get(rucc_url, timeout=120)
                resp.raise_for_status()
                data = resp.content
                try:
                    blob.upload_blob(data, overwrite=True)
                except Exception as upload_exc:
                    _log.warning("urban_flag: blob cache failed (%s)", upload_exc)

            wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            # USDA RUCC sheet structure (2023): columns include FIPS, State, County_Name, RUCC_2023.
            # The exact sheet name varies between releases; iterate sheets and
            # detect the one with the RUCC column.
            for sheet_name in wb.sheetnames:
                rows = wb[sheet_name].iter_rows(values_only=True)
                header = next(rows, None)
                if not header:
                    continue
                norm_header = [str(h or "").strip().lower() for h in header]
                # Find the FIPS column.
                fips_idx = None
                for cand in ("fips", "fips_code", "fipscode", "fips_txt"):
                    if cand in norm_header:
                        fips_idx = norm_header.index(cand)
                        break
                # Find the RUCC column (year suffix varies).
                rucc_idx = None
                for i, h in enumerate(norm_header):
                    if h.startswith("rucc_") or h == "rucc" or "ruralurbancontinuum" in h:
                        rucc_idx = i
                        break
                if fips_idx is None or rucc_idx is None:
                    continue
                _log.info(
                    "urban_flag: parsing sheet %r (fips col %d, rucc col %d)",
                    sheet_name, fips_idx, rucc_idx,
                )
                for row in rows:
                    try:
                        fips = str(row[fips_idx]).strip().zfill(5)
                        rucc = int(row[rucc_idx])
                    except (TypeError, ValueError, IndexError):
                        continue
                    if len(fips) != 5 or rucc < 1 or rucc > 9:
                        continue
                    self.rucc_urban[fips] = rucc <= self.URBAN_RUCC_MAX
                # Done with the first sheet that matched; don't double-count.
                break

            self.stats["rucc_loaded_counties"] = len(self.rucc_urban)
            _log.info(
                "urban_flag: loaded RUCC for %d counties (urban<=%d)",
                len(self.rucc_urban), self.URBAN_RUCC_MAX,
            )
        except Exception as exc:
            _log.exception("urban_flag fatal in load_data()")
            self.stats["load_data_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            raise

    # ── Stamp ────────────────────────────────────────────────────────────────
    def write_data_to_collection(self):
        from pymongo import MongoClient, UpdateOne
        from pymongo.errors import BulkWriteError

        db_name, coll_name = self.provider_collection.split(".", 1)
        client = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
            socketTimeoutMS=900_000,
        )
        ops: list = []
        try:
            self.coll = client[db_name][coll_name]
            _log.info(
                "urban_flag: states=%s; every addresses[*] entry (business or "
                "practice) whose own .state matches gets stamped; bulk_write "
                "batches of %d",
                self.states, self.bulk_batch_size,
            )
            for state in self.states:
                try:
                    cursor = self.coll.find(
                        {"addresses.state": state},
                        {"_id": 1, "npi": 1, "addresses": 1},
                    )
                    for provider in cursor:
                        self.stats["providers_scanned"] += 1
                        try:
                            for idx, addr in enumerate(provider.get("addresses") or []):
                                if not isinstance(addr, dict) or addr.get("state") != state:
                                    continue
                                self.stats["elements_scanned"] += 1
                                try:
                                    fips = (addr.get("county") or {}).get("fips")
                                    if not fips:
                                        self.stats["elements_no_county_fips"] += 1
                                        continue
                                    urban = self.rucc_urban.get(str(fips).zfill(5))
                                    if urban is None:
                                        self.stats["elements_unmatched_rucc"] += 1
                                        continue
                                    ops.append(UpdateOne(
                                        {"_id": provider["_id"]},
                                        {"$set": {f"addresses.{idx}.county.urban": urban}},
                                    ))
                                except Exception as exc:
                                    self.stats["element_exceptions"].append({
                                        "npi": provider.get("npi"),
                                        "idx": idx,
                                        "type": type(exc).__name__,
                                        "message": str(exc),
                                    })
                                if len(ops) >= self.bulk_batch_size:
                                    self._flush(ops)
                                    ops = []
                        except Exception as exc:
                            self.stats["provider_exceptions"].append({
                                "npi": provider.get("npi"),
                                "type": type(exc).__name__,
                                "message": str(exc),
                            })
                except Exception as exc:
                    self.stats["state_exceptions"].append({
                        "state": state,
                        "type": type(exc).__name__,
                        "message": str(exc),
                    })
            if ops:
                self._flush(ops)
        finally:
            client.close()

    def _flush(self, ops):
        from pymongo.errors import BulkWriteError
        try:
            result = self.coll.bulk_write(ops, ordered=False)
            self.stats["elements_stamped"] += result.modified_count
        except BulkWriteError as exc:
            details = exc.details or {}
            self.stats["elements_stamped"] += details.get("nModified", 0)
            for werr in details.get("writeErrors", []):
                self.stats["element_exceptions"].append({
                    "type": "BulkWriteError",
                    "code": werr.get("code"),
                    "message": str(werr.get("errmsg", ""))[:200],
                })
