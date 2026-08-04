# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared runtime helpers — collections, discrepancies, provider write target."""

from __future__ import annotations
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService


import os
from datetime import datetime, timezone
from typing import Any

from pipeline_config import load_pipeline_config
from pipeline_dataset_registry import PipelineDatasetRegistry
from pipeline_db import get_frontend_mongo, get_mongo
from staging_loader import staging_collection_name, staging_db_name


_log = ChatHealthyLoggingService()

# Every Provider write during the pipeline targets the STAGING collection
# on the pipeline cluster (staging_db + staging_coll_base come from
# dataset_versions[] in brain/machine_artifacts/content/pipeline_config.json,
# resolved via PipelineDatasetRegistry). Operator directive 2026-08-02:
# * Staging + loaded DB choices live in the registry, not in code.
# * Consumers must never observe partially-enriched Provider records; the
#   loaded collection stays at last-known-good until publish_provider does
#   the atomic renameCollection swap from the staging collection into the
#   public_data collection.
# * Load-state metadata for every loaded collection lives on the frontend
#   cluster (chathealthyfrontend.pipeline.loaded_metadata).
# The admin.command('renameCollection', ...) supports cross-DB rename, so
# the atomic swap between the two DBs stays a single server-side op.
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
        self._registry: PipelineDatasetRegistry | None = None

    @property
    def registry(self) -> PipelineDatasetRegistry:
        # Cached: the registry parses + fully validates the config's
        # dataset_versions[] array on construction, so building it once
        # per runtime avoids re-running the six structural invariants
        # every time a caller resolves a collection name.
        if self._registry is None:
            cfg = load_pipeline_config(env_prefix=self.env)
            self._registry = PipelineDatasetRegistry(cfg, self.data_version, self.mongo)
        return self._registry

    @property
    def provider_collection(self) -> str:
        # Registry owns the source->collection map. Provider writes go
        # to the STAGING collection during the fire (operator directive
        # 2026-08-02); publish_provider swaps staging into public_data
        # via cross-DB renameCollection at the end.
        entry = self.registry.by_source_name("provider")
        coll = self.registry.staging_collection_name("provider")
        return f"{entry.staging_db}.{coll}"

    @property
    def provider_staging_collection(self) -> str:
        entry = self.registry.by_source_name("provider")
        coll = self.registry.staging_collection_name("provider")
        return f"{entry.staging_db}.{coll}"

    @property
    def provider_public_data_collection(self) -> str:
        entry = self.registry.by_source_name("provider")
        coll = self.registry.public_data_collection_name("provider")
        return f"{entry.public_data_db}.{coll}"

    @property
    def smd_staging_collection(self) -> str:
        entry = self.registry.by_source_name("smd")
        coll = self.registry.staging_collection_name("smd")
        return f"{entry.staging_db}.{coll}"

    @property
    def smd_public_data_collection(self) -> str:
        entry = self.registry.by_source_name("smd")
        coll = self.registry.public_data_collection_name("smd")
        return f"{entry.public_data_db}.{coll}"

    @property
    def providers_coll(self):
        db_name, coll_name = self.provider_collection.split(".", 1)
        return self.mongo[db_name][coll_name]

    def staging_coll(self, source_name: str):
        # source_name is a dataset_versions[] entry name; the registry
        # resolves it to the versioned staging collection on the pipeline
        # cluster.
        return self.mongo[staging_db_name(self.registry, source_name)][
            staging_collection_name(self.registry, source_name)
        ]

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
        worker -- the state of its BUSINESS mailing (billing) address.
        Per LLD: business address is single-valued per NPI (NPPES
        contract) and is the canonical partition key. Practice addresses
        are optional and multi-valued (secondary practices) so cannot
        serve as the atomic key. $elemMatch on the single-valued business
        address gives exactly one owner per NPI."""
        if not state:
            return {"run_id": self.run_id}
        if state == "ALL_OTHERS":
            return {
                "run_id": self.run_id,
                "addresses": {"$elemMatch": {"address_type": "business",
                                             "state": {"$nin": list(STATE_US_SET)}}},
            }
        return {
            "run_id": self.run_id,
            "addresses": {"$elemMatch": {"address_type": "business", "state": state}},
        }

    def discrepancies_collection(self):
        return self.discrepancies_coll

    def reservations_collection(self):
        return self.frontend["admin"]["cluster_lifecycle"]
