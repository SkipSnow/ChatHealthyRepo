# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Medicare Provider Evaluation Data Pipeline (EPIC-010-F-007) — orchestrator.

Declares PIPELINE_NAME = "medicare" and the StepSpec DAG realized from the
LLD v83 Part III (III.10). Built on the generic run lifecycle (EPIC-010-F-001);
only the Medicare-specific steps and fan-out keys live here.

The FDA build is TWO formal steps (operator ruling 2026-10-20):
build_fda_label_resolution (record-range fan-out -> per-label results in the
transient collection) and finalize_fda_molecules (serial: groups by
molecule_id and writes the molecule-keyed fdaProductToDiagnosisResolution).
prepare_molecule_masters is the serial mode pre-pass: it stamps
partition_state (the prescriber's billing/business state) onto the staged
Part D rows, clears this data_version's published collections (full annual
replace) and measures the no-molecule bucket. Every step's timeout comes from
step_timeouts.medicare in the durable configuration (required; H7/H9).
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS)
from pipeline.run_lifecycle.base_pipeline_orchestrator import BasePipelineOrchestrator
from pipeline.run_lifecycle.step_spec import StepSpec

_log = ChatHealthyLoggingService()


class MedicareProviderEvaluationOrchestrator(BasePipelineOrchestrator):
    PIPELINE_NAME = "medicare"
    PIPELINE_DISPLAY_NAME = "Medicare Prescription and Procedures Pipeline"

    STEPS: list[StepSpec] = [
        StepSpec(
            name="prepare_infrastructure",
            prerequisites=[],
            parallelism="serial",
            always_run=True,
            aca_job_name="med-prepare-infrastructure",
        ),
        StepSpec(
            name="medicare_source_freshness_gate",
            prerequisites=["prepare_infrastructure"],
            parallelism="serial",
            always_run=True,
        ),
        StepSpec(
            name="medicare_fetch_all_sources",
            prerequisites=["medicare_source_freshness_gate"],
            parallelism="process_pool",
            aca_job_name="med-fetch-sources",
            partition_key="source_name_base",
        ),
        StepSpec(
            name="medicare_source_archival",
            prerequisites=["medicare_fetch_all_sources"],
            parallelism="serial",
        ),
        StepSpec(
            name="medicare_load_staging",
            prerequisites=["medicare_source_archival"],
            parallelism="process_pool",
            aca_job_name="med-load-staging",
            partition_key="source_name",
        ),
        StepSpec(
            # [FDA] Index this run's prescription FDA labels (non-OTC) with a
            # sequential fda_record_number so the per-label resolution can
            # range-partition its work (LLD I.3.4a). Serial, once. Reports the
            # in-scope label count (the conformance arithmetic's denominator).
            name="index_fda_prescription_records",
            prerequisites=["medicare_load_staging"],
            parallelism="serial",
            aca_job_name="med-index-fda-records",
        ),
        StepSpec(
            # [FDA 1/2] Per-label resolution fan-out (LLD v83 III.10): one
            # worker PROCESS per record-number range (width from
            # fan_out.medicare.build_fda_label_resolution); per-label results
            # land in the transient collection, idempotently by set_id.
            # Publishes nothing. Gated by the build_fda_drug arg.
            name="build_fda_label_resolution",
            prerequisites=["index_fda_prescription_records"],
            parallelism="process_pool",
            aca_job_name="med-build-fda-labels",
            partition_key="record_range",
        ),
        StepSpec(
            # [FDA 2/2] Serial molecule finalize (LLD v83 III.10): groups the
            # label results by molecule_id and writes the MOLECULE-keyed
            # fdaProductToDiagnosisResolution (_id = molecule_id, set_ids =
            # the folded labels). Clears this data_version's collection first
            # (full annual replace). Orphan labels are stamped + findings (H2,
            # non-fatal).
            name="finalize_fda_molecules",
            prerequisites=["build_fda_label_resolution"],
            parallelism="serial",
            aca_job_name="med-finalize-fda-molecules",
        ),
        StepSpec(
            # Serial: the annual full replace (REQ-B-015). Clears this
            # data_version's published collections before the base fan-out; a
            # clear inside a state worker would wipe its peers' records.
            name="clear_published_collections",
            prerequisites=["medicare_load_staging"],
            parallelism="serial",
            aca_job_name="med-clear-published",
        ),
        StepSpec(
            # CMS_NPI_PrescriberBase: one worker per CMS state, reading the
            # staged Part D rows and NO other source. Groups by (NPI,
            # molecule) and applies one gated, mergeable contribution per
            # group (REQ-B-001, REQ-B-006). The row carries its own partition
            # key: no NPI's rows span two CMS states (0 of 28,023,892,
            # measured 2026-10-23).
            name="build_providermedicare_base",
            prerequisites=["clear_published_collections"],
            parallelism="process_pool",
            aca_job_name="med-build-providermedicare",
            partition_key="cms_state",
        ),
        StepSpec(
            # One enrichment pass per source-state partition over the base
            # collection: the Provider collection's billing state, entity type
            # and NUCC codes, an organization's CCN and size tier, and the FDA
            # molecule block and indications. One sweep, not one per fact --
            # the collection holds 26.7M records.
            name="enrich",
            prerequisites=["build_providermedicare_base",
                           "finalize_fda_molecules"],
            parallelism="process_pool",
            aca_job_name="med-enrich",
            partition_key="source_state",
        ),
        StepSpec(
            # The NUCC rollup, read from the staged Part D rows.
            name="rollup_specialtymedicare",
            prerequisites=["enrich"],
            parallelism="process_pool",
            aca_job_name="med-rollup-specialtymedicare",
            partition_key="nucc_code",
        ),
        StepSpec(
            # The audits -- one per published collection. ProviderMedicare by
            # deterministic cross-section sample recomputed against its source
            # rows; the FDA and specialty collections in full.
            name="medicare_contract_conformance",
            prerequisites=["enrich", "rollup_specialtymedicare"],
            parallelism="serial",
            aca_job_name="med-audit-collections",
        ),
        StepSpec(
            # Last: the partition keys the work needed and the served
            # collection does not. After the audits, which still recompute a
            # sampled record against the rows it was built from.
            name="drop_transient_attributes",
            prerequisites=["medicare_contract_conformance"],
            parallelism="process_pool",
            aca_job_name="med-drop-transient",
            partition_key="source_state",
        ),
        StepSpec(
            name="quiesce_infrastructure",
            prerequisites=[],
            parallelism="serial",
            invocation_phase="finally_block",
        ),
    ]

    # The Medicare instance declares its own source set and fan-out dimensions.
    # Fetchable sources declared in pipeline_config dataset_versions[]. The two
    # Coverage DB tables (HCPCS and ICD-10) each fetch the same Coverage DB
    # archive and extract their own member from it (fetch.extract, a
    # cascading-zip member path), so each is an independent fetched source.
    FETCH_SOURCES = [
        "medicare_partb",
        "medicare_partd",
        "ccsr",
        "icd10_cm",
        "openfda_labels",
        "medicare_pos",
        "medicare_coverage_hcpc",
        "medicare_coverage_icd10",
        "rxnorm",
    ]
    # Every fetched source also lands in staging; nothing is load-only now.
    LOAD_SOURCES = FETCH_SOURCES

    # The DATA each step consumes (operator design 2026-10-22): source name ->
    # resolved staging collection, checked nonempty by StepDataDependenciesAreMet
    # before the step runs. EVERY step answers here -- a step that consumes no
    # collection (infra, fetch, gates) declares [] explicitly, and a step name
    # this map does not know is an unimplemented-check fatal, never a pass.
    # "transient" is the FDA per-label result store; "fda_published" the
    # molecule collection; "masters" the ProviderMedicare build. No run_id
    # enters any check: a full load drains-then-replaces every staging
    # collection, so existence-with-rows IS the dependency.
    STEP_DATA_DEPENDENCIES: dict[str, list[str]] = {
        "prepare_infrastructure": [],
        "medicare_source_freshness_gate": [],
        "medicare_fetch_all_sources": [],
        "medicare_source_archival": [],
        "medicare_load_staging": [],
        "index_fda_prescription_records": ["staging:openfda_labels"],
        "build_fda_label_resolution": ["fda_stamped"],
        "finalize_fda_molecules": ["if_build_fda:transient",
                                   "if_build_fda:staging:rxnorm",
                                   "if_consume_fda:fda_published"],
        "clear_published_collections": [],
        "build_providermedicare_base": ["staging:medicare_partd"],
        "enrich": ["masters", "provider_spine", "fda_published",
                   "staging:medicare_pos"],
        "rollup_specialtymedicare": ["staging:medicare_partd"],
        "medicare_contract_conformance": ["masters", "specialty_published",
                                          "fda_published"],
        "drop_transient_attributes": ["masters"],
        "quiesce_infrastructure": [],
    }

    def StepDataDependenciesAreMet(self, step_name: str, ctx) -> bool:
        if step_name not in self.STEP_DATA_DEPENDENCIES:
            raise ChatHealthyException(
                mode="not_implemented",
                message=(f"MedicareProviderEvaluationOrchestrator declares no "
                         f"data dependencies for step {step_name!r}; every "
                         f"step must answer StepDataDependenciesAreMet"))
        deps = self.STEP_DATA_DEPENDENCIES[step_name]
        if not deps:
            return True
        from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime  # noqa: PLC0415
        from pipeline.MedicareProviderEvaluationDataPipeline.steps.build_fda_label_resolution import (  # noqa: PLC0415
            TRANSIENT_COLLECTION)
        reg = PipelineRuntime(ctx).registry
        mongo = ctx.mongo_client
        building = bool(ctx.args.build_fda_drug)
        for dep in deps:
            if dep.startswith("if_build_fda:"):
                if not building:
                    continue
                dep = dep.split(":", 1)[1]
            elif dep.startswith("if_consume_fda:"):
                if building:
                    continue
                dep = dep.split(":", 1)[1]
            if dep.startswith("staging:"):
                src = dep.split(":", 1)[1]
                e = reg.by_source_name(src)
                coll = mongo[e.staging_db][reg.staging_collection_name(src)]
            elif dep == "fda_stamped":
                ofda = reg.by_source_name("openfda_labels")
                coll = mongo[ofda.staging_db][reg.staging_collection_name("openfda_labels")]
                if coll.count_documents({"fda_record_number": {"$exists": True}},
                                        limit=1) == 0:
                    _log.LogPipeline(
                        "ERROR", "StepDataDependenciesAreMet: step=%s needs "
                        "STAMPED FDA labels (fda_record_number) and the staged "
                        "openFDA collection carries none", step_name)
                    return False
                continue
            elif dep == "partd_stamped":
                partd = reg.by_source_name("medicare_partd")
                coll = mongo[partd.staging_db][reg.staging_collection_name("medicare_partd")]
                if coll.count_documents({"partition_state": {"$exists": True}},
                                        limit=1) == 0:
                    _log.LogPipeline(
                        "ERROR", "StepDataDependenciesAreMet: step=%s needs "
                        "partition-STAMPED Part D rows (partition_state) and "
                        "the staged collection carries none", step_name)
                    return False
                continue
            elif dep == "transient":
                ofda = reg.by_source_name("openfda_labels")
                coll = mongo[ofda.staging_db][TRANSIENT_COLLECTION]
            elif dep == "fda_published":
                tgt = reg.by_source_name("fda_drug")
                coll = mongo[tgt.public_data_db][reg.public_data_collection_name("fda_drug")]
            elif dep == "masters":
                tgt = reg.by_source_name("providermedicare")
                coll = mongo[tgt.public_data_db][reg.public_data_collection_name("providermedicare")]
            elif dep == "specialty_published":
                tgt = reg.by_source_name("specialtymedicare")
                coll = mongo[tgt.public_data_db][reg.public_data_collection_name("specialtymedicare")]
            elif dep == "provider_spine":
                from pipeline.MedicareProviderEvaluationDataPipeline.provider_source import (  # noqa: PLC0415
                    parse_provider_source)
                db_name, coll_name = parse_provider_source(ctx.provider_collection)
                coll = mongo[db_name][coll_name]
            else:
                raise ChatHealthyException(
                    mode="not_implemented",
                    message=(f"StepDataDependenciesAreMet: step {step_name!r} "
                             f"declares unknown dependency kind {dep!r}"))
            if coll.count_documents({}, limit=1) == 0:
                _log.LogPipeline(
                    "ERROR", "StepDataDependenciesAreMet: step=%s dependency=%s "
                    "collection=%s is absent or empty", step_name, dep, coll.full_name)
                return False
        return True

    def _partition_enumerators(self):
        # The fan-out dimension and worker cap for EVERY step are declared in
        # persistent config (fan_out.medicare.<step>) per §I.3.4. This method
        # only supplies the code hooks the config names: the Medicare
        # record-range enumerator, merged onto the base geographic enumerators
        # (state, county, state_entity).
        enums = dict(super()._partition_enumerators())
        enums["record_ranges"] = self._fda_record_ranges
        enums["cms_states"] = self._cms_states
        enums["source_states"] = self._source_states
        return enums

    def _cms_states(self, ctx, **_) -> list[dict]:
        """The CMS states present in the staged Part D rows -- the base task's
        fan-out. Read from the data, not declared: the row carries its own
        Prscrbr_State_Abrvtn and no NPI's rows span two states (0 of
        28,023,892, measured 2026-10-23)."""
        from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime  # noqa: PLC0415
        reg = PipelineRuntime(ctx).registry
        partd = reg.by_source_name("medicare_partd")
        coll = ctx.mongo_client[partd.staging_db][
            reg.staging_collection_name("medicare_partd")]
        field = f"raw.{PARTD_COLS['state']}"
        states = [d["_id"] for d in coll.aggregate(
            [{"$group": {"_id": f"${field}"}}, {"$sort": {"_id": 1}}],
            allowDiskUse=True) if d.get("_id")]
        return [{"cms_state": s} for s in states]

    def _source_states(self, ctx, **_) -> list[dict]:
        """The source states carried on the built records -- the fan-out for
        every step that reads the base collection."""
        from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime  # noqa: PLC0415
        reg = PipelineRuntime(ctx).registry
        tgt = reg.by_source_name("providermedicare")
        coll = ctx.mongo_client[tgt.public_data_db][
            reg.public_data_collection_name("providermedicare")]
        states = [d["_id"] for d in coll.aggregate(
            [{"$group": {"_id": "$source_state"}}, {"$sort": {"_id": 1}}],
            allowDiskUse=True) if d.get("_id")]
        return [{"source_state": s} for s in states]

    def _fda_record_ranges(self, ctx, *, count: int, **_) -> list[dict]:
        """Even record-number ranges over this run's stamped prescription FDA
        labels -- one worker PROCESS per range (LLD I.3.4a). Off-build returns
        a single verify/consume partition. The width (count) is REQUIRED from
        fan_out.medicare.build_fda_label_resolution.params (H7: no defaults).
        Reads the gate from ctx.args (the work-item snapshot), the same source
        the step reads -- never the environment."""
        if not bool(ctx.args.build_fda_drug):
            return [{"single": True}]
        from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
        reg = PipelineRuntime(ctx).registry
        ofda = reg.by_source_name("openfda_labels")
        staging = ctx.mongo_client[ofda.staging_db][reg.staging_collection_name("openfda_labels")]
        total = staging.count_documents(
            {"fda_record_number": {"$exists": True}})
        if total <= 0:
            return [{"single": True}]
        worker_count = max(1, min(int(count), total))
        size = -(-total // worker_count)  # ceil: ~worker_count even ranges, one per process
        return [{"record_range": {"start": s, "end": min(s + size, total + 1)}}
                for s in range(1, total + 1, size)]
