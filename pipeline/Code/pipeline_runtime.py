# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared runtime helpers — collections, discrepancies, provider write target."""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from pipeline_config import load_pipeline_config
from pipeline_db import get_frontend_mongo, get_mongo

_log = logging.getLogger("pipeline_runtime")

STAGING = {
    "nppes_npi": "pipeline_sources_nppes_npi",
    "pl_pfile": "pipeline_sources_pl_pfile",
    "nucc_taxonomy": "pipeline_sources_nucc_taxonomy",
    "zip_county_crosswalk": "pipeline_sources_zip_county_crosswalk",
    "rucc": "pipeline_sources_rucc",
    "specialty_catalog": "pipeline_sources_specialty_catalog",
}

REPORTS_CONTAINER_SUFFIX = "-pipeline-reports"

STATE_US_SET = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID", "IL", "IN",
    "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT", "NE", "NV", "NH",
    "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT",
    "VT", "VA", "WA", "WV", "WI", "WY",
}


class PipelineRuntime:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.mongo = ctx.mongo_client or get_mongo()
        self.frontend = get_frontend_mongo()
        self.env = ctx.env_prefix
        self.run_id = ctx.run_id
        self.db = self.mongo[f"{self.env}_PublicHealthData"]
        self._provider_collection: str | None = None

    @property
    def provider_collection(self) -> str:
        if self._provider_collection:
            return self._provider_collection
        cfg = load_pipeline_config(self.frontend, self.env)
        target = (
            cfg.get("dataset_versions", {}).get("provider_write_target")
            or self.ctx.args.provider_write_target()
        )
        if "." not in target:
            target = f"{self.env}_PublicHealthData.{target}"
        self._provider_collection = target
        return target

    @property
    def providers_coll(self):
        db_name, coll_name = self.provider_collection.split(".", 1)
        return self.mongo[db_name][coll_name]

    def staging_coll(self, key: str):
        return self.db[STAGING[key]]

    @property
    def discrepancies_coll(self):
        coll_name = "pipeline.discrepancies"
        if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
            from pipeline_test_config import TEST_DISCREPANCIES_COLL
            coll_name = TEST_DISCREPANCIES_COLL.split(".", 1)[-1]
        return self.frontend["chathealthyfrontend"][coll_name]

    @property
    def runs_coll(self):
        coll_name = "pipeline.runs"
        if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
            from pipeline_test_config import TEST_RUNS_COLL
            coll_name = TEST_RUNS_COLL.split(".", 1)[-1]
        return self.frontend["chathealthyfrontend"][coll_name]

    @property
    def reports_container(self) -> str:
        return f"{self.env}{REPORTS_CONTAINER_SUFFIX}"

    def record_discrepancy(
        self,
        *,
        npi: str | None,
        reason: str,
        step: str,
        state: str | None = None,
        entity_kind: str | None = None,
        detail: dict | None = None,
    ) -> None:
        self.discrepancies_coll.insert_one({
            "run_id": self.run_id,
            "npi": npi,
            "reason": reason,
            "step": step,
            "state": state,
            "entity_kind": entity_kind,
            "detail": detail or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        })

    def mailing_state(self, doc: dict) -> str | None:
        for addr in doc.get("addresses") or []:
            if isinstance(addr, dict) and addr.get("address_type") == "mailing":
                return (addr.get("state") or "").upper() or None
        return None

    def entity_kind(self, doc: dict) -> str:
        etc = doc.get("entity_type_code", "")
        if etc == "2":
            return "institutional"
        return "individual"

    def partition_filter(self, state: str) -> dict:
        if not state:
            return {"run_id": self.run_id}
        if state == "ALL_OTHERS":
            return {
                "run_id": self.run_id,
                "addresses": {"$elemMatch": {
                    "address_type": "mailing",
                    "state": {"$nin": list(STATE_US_SET)},
                }},
            }
        return {
            "run_id": self.run_id,
            "addresses": {"$elemMatch": {"state": state}},
        }

    def discrepancies_collection(self):
        return self.discrepancies_coll

    def reservations_collection(self):
        return self.frontend["admin"]["cluster_lifecycle"]
