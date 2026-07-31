# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared runtime helpers — collections, discrepancies, provider write target."""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


import os
from datetime import datetime, timezone
from typing import Any

from pipeline_config import load_pipeline_config
from pipeline_db import get_frontend_mongo, get_mongo
from staging_loader import STAGING_DB_NAME, staging_collection_name


_log = ChatHealthyLoggingService()

# Default base name (db.coll, WITHOUT the _v_<data_version> suffix) for the
# provider write target. Runtime appends _v_{data_version} at read time so
# a data_version bump does not require a config change. The Mongo
# pipeline.config MAY override this base with its own dataset_versions
# .provider_write_target entry (base name only, no version).
_DEFAULT_PROVIDER_TARGET_BASE = "PublicData.Provider"

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
        self.data_version = int(ctx.args.data_version)
        self.staging_db = self.mongo[STAGING_DB_NAME]
        self._provider_collection: str | None = None

    @property
    def provider_collection(self) -> str:
        if self._provider_collection:
            return self._provider_collection
        cfg = load_pipeline_config(self.frontend, self.env)
        base = (
            cfg.get("dataset_versions", {}).get("provider_write_target")
            or _DEFAULT_PROVIDER_TARGET_BASE
        )
        # Config carries the base name (db.coll, no version suffix); the
        # runtime appends _v_{data_version} so we never rewrite the config
        # to bump a version.
        target = f"{base}_v_{self.data_version}"
        self._provider_collection = target
        return target

    @property
    def providers_coll(self):
        db_name, coll_name = self.provider_collection.split(".", 1)
        return self.mongo[db_name][coll_name]

    def staging_coll(self, source_name: str):
        # source_name matches staging_loader.STAGING_BASE_NAMES keys.
        # staging_collection_name() returns the base + _v_{data_version}.
        return self.staging_db[staging_collection_name(source_name, self.data_version)]

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
        """NPI-atomic ownership: a provider is owned by exactly ONE state
        worker -- the state of its PRIMARY practice (addresses[0], which
        normalize_provider_rows guarantees is the NPPES primary practice
        location). This closes the class of bug where a multi-state
        provider (practice in DE, secondary in PA) appeared in BOTH DE
        and PA workers' queries, causing full-array $set clobber on the
        shared doc. See NPI 1962405589 in run c8080b for the real-world
        instance. Rule 2026-07-31: never partition below the NPI."""
        if not state:
            return {"run_id": self.run_id}
        if state == "ALL_OTHERS":
            return {
                "run_id": self.run_id,
                "addresses.0.address_type": "practice",
                "addresses.0.state": {"$nin": list(STATE_US_SET)},
            }
        return {
            "run_id": self.run_id,
            "addresses.0.address_type": "practice",
            "addresses.0.state": state,
        }

    def discrepancies_collection(self):
        return self.discrepancies_coll

    def reservations_collection(self):
        return self.frontend["admin"]["cluster_lifecycle"]
