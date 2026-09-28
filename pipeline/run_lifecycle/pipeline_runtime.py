# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Shared runtime helpers — collections, discrepancies, provider write target."""

from __future__ import annotations
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService


import os
from datetime import datetime, timezone
from typing import Any

from pymongo import UpdateOne

from pipeline.run_lifecycle.pipeline_config import load_pipeline_config
from pipeline.run_lifecycle.pipeline_dataset_registry import PipelineDatasetRegistry
from pipeline.provider_base.staging_loader import staging_collection_name, staging_db_name
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities


_log = ChatHealthyLoggingService()

# One unified control-store collection holds both document kinds the
# discrepancy-report infrastructure uses (LLD v54 §7.5): per-record `record`
# documents and per-finding-class `type_aggregate` documents.
PIPELINE_ADMIN_DB = "pipelineAdmin"
DISCREPANCY_LOG_COLLECTION = "discrepancyLog"


def get_mongo(identity: str = "pipelineEditor",
              cluster: str = "ChatHealthyDataPipelines"):
    return ChatHealthyMongoUtilities().getConnection(identity, cluster)


def get_frontend_mongo(identity: str = "pipelineEditor"):
    return ChatHealthyMongoUtilities().getConnection(identity, "ChatHealthyFrontEnd")


def load_discrepancy_config(frontend_mongo, pipeline_name: str) -> dict:
    """The pipeline's discrepancy_report block from the control store.

    Read from pipelineAdmin.PipelineConfig (_id=<pipeline_name>), seeded by
    seed_pipeline_config.py. Carries finding_types (the severity map),
    business_record_keys, and report_cap_per_class.
    """
    doc = frontend_mongo[PIPELINE_ADMIN_DB]["PipelineConfig"].find_one(
        {"_id": pipeline_name}) or {}
    return doc.get("discrepancy_report") or {}


def _severity_for(dr_cfg: dict, finding_class: str) -> str:
    """Severity is configuration, and configuration is authoritative (§7.5).

    A class the run emits that finding_types does not name is rejected at
    write time rather than silently graded.
    """
    entry = ((dr_cfg or {}).get("finding_types") or {}).get(finding_class)
    if not entry or not entry.get("severity"):
        raise ChatHealthyException(
            mode="pipeline_finding_class_unrecognized",
            component="PipelineRuntime",
            message=(
                f"finding class {finding_class!r} is not declared in the "
                f"pipeline's discrepancy_report.finding_types; a run may emit "
                f"only declared classes, and adding one is an operator-approved "
                f"configuration change"
            ),
            finding_class=finding_class,
        )
    return entry["severity"]


def build_discrepancy_ops(
    *,
    run_id: str,
    artifact: str,
    record_key: Any,
    record_key_kind: str | None,
    finding_class: str,
    severity: str,
    stage: str,
    detail: dict | None,
    recorded_at: str,
) -> list[UpdateOne]:
    """The two atomic upserts one finding records (§7.5 write semantics).

    A per-record `record` document (findings appended, grade added to
    severities_present) and a per-class `type_aggregate` document (record key
    added to keys, count incremented). Both are server-side
    $push/$addToSet/$inc/$setOnInsert so ~100 concurrent Workers never lose a
    finding to a client read-modify-write.
    """
    rk = str(record_key)
    record_id = f"{run_id}:{artifact}:{rk}"
    aggregate_id = f"{run_id}:{artifact}:type:{finding_class}"
    finding = {
        "class": finding_class,
        "severity": severity,
        "stage": stage,
        "detail": detail or {},
    }
    record_op = UpdateOne(
        {"_id": record_id},
        {
            "$setOnInsert": {
                "kind": "record",
                "run_id": run_id,
                "artifact": artifact,
                "record_key_kind": record_key_kind,
                "record_key": rk,
                "recorded_at": recorded_at,
            },
            "$push": {"findings": finding},
            "$addToSet": {"severities_present": severity},
        },
        upsert=True,
    )
    aggregate_op = UpdateOne(
        {"_id": aggregate_id},
        {
            "$setOnInsert": {
                "kind": "type_aggregate",
                "run_id": run_id,
                "artifact": artifact,
                "class": finding_class,
                "recorded_at": recorded_at,
            },
            "$set": {"severity": severity},
            "$addToSet": {"keys": rk},
            "$inc": {"count": 1},
        },
        upsert=True,
    )
    return [record_op, aggregate_op]


def write_finding(
    log_coll,
    dr_cfg: dict,
    *,
    run_id: str,
    artifact: str,
    record_key: Any,
    finding_class: str,
    stage: str,
    detail: dict | None = None,
) -> str:
    """Record one finding into discrepancyLog and abort the run if it is fatal.

    Returns the finding's configured severity. Raises
    pipeline_finding_class_unrecognized for an undeclared class, and
    pipeline_fatal_finding when the class is graded fatal or its running count
    reaches the configured fatal_at_count — the run's single fatal (§7.5).
    """
    severity = _severity_for(dr_cfg, finding_class)
    record_key_kind = (dr_cfg.get("business_record_keys") or {}).get(artifact)
    recorded_at = datetime.now(timezone.utc).isoformat()
    log_coll.bulk_write(
        build_discrepancy_ops(
            run_id=run_id, artifact=artifact, record_key=record_key,
            record_key_kind=record_key_kind, finding_class=finding_class,
            severity=severity, stage=stage, detail=detail,
            recorded_at=recorded_at,
        ),
        ordered=False,
    )
    fatal_at_count = ((dr_cfg.get("finding_types") or {})
                      .get(finding_class, {}).get("fatal_at_count"))
    count = None
    is_fatal = severity == "fatal"
    if not is_fatal and fatal_at_count is not None:
        aggregate = log_coll.find_one(
            {"_id": f"{run_id}:{artifact}:type:{finding_class}"}, {"count": 1})
        count = (aggregate or {}).get("count")
        is_fatal = count is not None and count >= fatal_at_count
    if is_fatal:
        # already_recorded_fatal marks this event as ALREADY captured in
        # discrepancyLog as a domain-class fatal aggregate, so the downstream
        # fatal recorders do not add a second (job-level) fatal marker for the
        # same event -- a run records and reports exactly one fatal (§7.5).
        raise ChatHealthyException(
            mode="pipeline_fatal_finding",
            component="PipelineRuntime",
            message=(
                f"finding class {finding_class!r} on {artifact} record "
                f"{record_key!r} is fatal and aborts the run"
            ),
            finding_class=finding_class,
            severity=severity,
            artifact=artifact,
            record_key=str(record_key),
            step=stage,
            count=count,
            fatal_at_count=fatal_at_count,
            already_recorded_fatal=True,
        )
    return severity

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
#   cluster (pipelineAdmin.pipeline.loaded_metadata).
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
        self._dr_cfg: dict | None = None

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
    def discrepancy_log_coll(self):
        if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
            from pipeline.run_lifecycle.pipeline_test_config import TEST_DISCREPANCY_LOG_COLL
            return self.frontend[PIPELINE_ADMIN_DB][TEST_DISCREPANCY_LOG_COLL.split(".", 1)[-1]]
        return self.frontend[PIPELINE_ADMIN_DB][DISCREPANCY_LOG_COLLECTION]

    @property
    def discrepancy_config(self) -> dict:
        if self._dr_cfg is None:
            self._dr_cfg = load_discrepancy_config(
                self.frontend, self.ctx.manifest.pipeline_name)
        return self._dr_cfg

    @property
    def runs_coll(self):
        if os.environ.get("PIPELINE_TEST_MODE", "").lower() in ("1", "true", "yes"):
            from pipeline.run_lifecycle.pipeline_test_config import TEST_RUNS_COLL
            return self.frontend["pipelineAdmin"][TEST_RUNS_COLL.split(".", 1)[-1]]
        return self.frontend["pipelineAdmin"]["pipeline.runs"]

    @property
    def reports_container(self) -> str:
        return f"{self.env}{REPORTS_CONTAINER_SUFFIX}"

    def record_discrepancy(
        self,
        *,
        npi: str | None = None,
        reason: str,
        step: str,
        state: str | None = None,
        entity_kind: str | None = None,
        detail: dict | None = None,
        artifact: str = "provider",
        record_key: Any = None,
    ) -> None:
        """Record one per-record finding into pipelineAdmin.discrepancyLog.

        Provider callers rely on the artifact="provider" default and the
        record_key defaulting to the NPI. The severity is read from the
        pipeline's finding_types config; a fatal finding aborts the run.
        """
        key = record_key if record_key is not None else npi
        full_detail = dict(detail or {})
        if state is not None:
            full_detail.setdefault("state", state)
        if entity_kind is not None:
            full_detail.setdefault("entity_kind", entity_kind)
        write_finding(
            self.discrepancy_log_coll,
            self.discrepancy_config,
            run_id=self.run_id,
            artifact=artifact,
            record_key=key,
            finding_class=reason,
            stage=step,
            detail=full_detail,
        )

    def mailing_state(self, doc: dict) -> str | None:
        addr = doc.get("business_address")
        if isinstance(addr, dict):
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
        from pipeline.run_lifecycle.steps._partitions import business_state_filter  # noqa: PLC0415
        return {"run_id": self.run_id, **business_state_filter(state)}

    def discrepancy_log_collection(self):
        return self.discrepancy_log_coll

    def reservations_collection(self):
        return self.frontend["pipelineAdmin"]["cluster_lifecycle"]
