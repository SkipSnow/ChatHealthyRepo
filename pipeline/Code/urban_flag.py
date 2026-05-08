import io
import logging
import os

import requests

_log = logging.getLogger("urban_flag")


def stamp_urban_flag_fn(config):
    stamper = UrbanFlagStamper(**config)
    stamper.run()
    result = {"summary": stamper.stats}
    return result


class UrbanFlagStamper:
    CENSUS_URL = "https://www2.census.gov/geo/docs/reference/ua/2020_UA_COUNTY.xlsx"
    CENSUS_BLOB_NAME = "census_2020_ua_county.xlsx"
    URBAN_THRESHOLD = 0.5

    def __init__(self, blob_container, states, provider_collection, bulk_batch_size):
        self.blob_container = blob_container
        self.states = states
        self.provider_collection = provider_collection
        self.bulk_batch_size = bulk_batch_size
        self.census = {}
        self.stats = {
            "providers_scanned": 0,
            "elements_scanned": 0,
            "elements_stamped": 0,
            "elements_no_county_fips": 0,
            "elements_unmatched_census": 0,
            "state_exceptions": [],
            "provider_exceptions": [],
            "element_exceptions": [],
        }

    def run(self):
        self.load_data()
        self.write_data_to_collection()

    def load_data(self):
        try:
            from blob_client import get_blob_service
            from openpyxl import load_workbook

            blob = (get_blob_service()
                    .get_container_client(self.blob_container)
                    .get_blob_client(self.CENSUS_BLOB_NAME))
            try:
                data = blob.download_blob().readall()
            except Exception as exc:
                _log.info("urban_flag: blob miss (%s) — fetching Census", exc)
                resp = requests.get(self.CENSUS_URL, timeout=120)
                resp.raise_for_status()
                data = resp.content
                try:
                    blob.upload_blob(data, overwrite=True)
                except Exception as upload_exc:
                    _log.warning("urban_flag: blob cache failed (%s)", upload_exc)

            wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
            for sheet_name in ("2020_UA_COUNTY", "CT_2022"):
                if sheet_name not in wb.sheetnames:
                    continue
                rows = wb[sheet_name].iter_rows(values_only=True)
                header = next(rows, None)
                if not header:
                    continue
                try:
                    i_state = header.index("STATE")
                    i_county = header.index("COUNTY")
                    i_pct = header.index("POPPCT_URB")
                except ValueError:
                    continue
                for row in rows:
                    try:
                        state = str(row[i_state]).strip().zfill(2)
                        county = str(row[i_county]).strip().zfill(3)
                        pct = float(row[i_pct])
                    except (TypeError, ValueError, IndexError):
                        continue
                    fips = state + county
                    if len(fips) == 5:
                        self.census[fips] = pct >= self.URBAN_THRESHOLD
        except Exception as exc:
            _log.exception("urban_flag fatal in load_data()")
            self.stats["load_data_error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }

    def write_data_to_collection(self):
        from pymongo import MongoClient, UpdateOne
        from pymongo.errors import BulkWriteError

        db_name, coll_name = self.provider_collection.split(".", 1)
        client = MongoClient(
            os.environ["MONGO_connectionString"],
            serverSelectionTimeoutMS=120_000,
            socketTimeoutMS=900_000,
        )
        ops = []
        try:
            self.coll = client[db_name][coll_name]
            print(f"urban_flag: states arg = {self.states} "
                  f"(iterating one cursor per state; only practice_address "
                  f"elements whose own .state matches get stamped; "
                  f"bulk_write batches of {self.bulk_batch_size})",
                  flush=True)
            for state in self.states:
                print(f"urban_flag: state={state} "
                      f"providers_scanned={self.stats['providers_scanned']} "
                      f"elements_stamped={self.stats['elements_stamped']}",
                      flush=True)
                try:
                    for provider in self.coll.find(
                        {"practice_address.state": state},
                        {"_id": 1, "npi": 1, "practice_address": 1},
                    ):
                        self.stats["providers_scanned"] += 1
                        try:
                            for idx, addr in enumerate(provider.get("practice_address") or []):
                                if not isinstance(addr, dict) or addr.get("state") != state:
                                    continue
                                self.stats["elements_scanned"] += 1
                                try:
                                    fips = (addr.get("county") or {}).get("fips")
                                    if not fips:
                                        self.stats["elements_no_county_fips"] += 1
                                        continue
                                    urban = self.census.get(fips)
                                    if urban is None:
                                        self.stats["elements_unmatched_census"] += 1
                                        continue
                                    ops.append(UpdateOne(
                                        {"_id": provider["_id"]},
                                        {"$set": {f"practice_address.{idx}.county.urban": urban}},
                                    ))
                                except Exception as exc:
                                    self.stats["element_exceptions"].append({
                                        "npi": provider.get("npi"),
                                        "idx": idx,
                                        "type": type(exc).__name__,
                                        "message": str(exc),
                                    })
                                if len(ops) >= self.bulk_batch_size:
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
        finally:
            client.close()
