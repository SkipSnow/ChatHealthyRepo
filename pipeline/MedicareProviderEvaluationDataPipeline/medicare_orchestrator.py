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
replace), writes the master stubs with state_aggregated decided globally, and
measures the no-molecule bucket. Every step's timeout comes from
step_timeouts.medicare in the durable configuration (required; H7/H9).
"""

from __future__ import annotations

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

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
            aca_job_name="med-prepare-infrastructure",
        ),
        StepSpec(
            name="medicare_source_freshness_gate",
            prerequisites=["prepare_infrastructure"],
            parallelism="serial",
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
            # [BASE 0/2] The serial mode pre-pass (LLD v83 III.10): stamps
            # partition_state (billing/business state by NPI, III.3) onto the
            # staged Part D rows, clears this data_version's published
            # collections (decision #1), writes every master stub with
            # state_aggregated decided from the GLOBAL per-molecule count and
            # applied_partitions [], and measures/reports the no-molecule
            # bucket (III.6).
            name="prepare_molecule_masters",
            prerequisites=["medicare_load_staging"],
            parallelism="serial",
            aca_job_name="med-prepare-molecule-masters",
        ),
        StepSpec(
            # [BASE 1/2] The master apply fan-out (LLD v83 III.3): partitions
            # are billing/business state x entity type; each worker groups its
            # partition's rows per molecule and applies ONE ATOMIC UPDATE per
            # (molecule, partition) -- children + $inc totals, gated by the
            # applied_partitions marker (re-application is a silent no-op).
            name="build_providermedicare_base",
            prerequisites=["prepare_molecule_masters"],
            parallelism="process_pool",
            aca_job_name="med-build-providermedicare",
            partition_key="partition_state",
        ),
        StepSpec(
            # [ENRICH] Serial: attaches each master's fda block from its
            # molecule-keyed FDA record by _id (point lookup).
            name="enrich_drugs",
            prerequisites=["build_providermedicare_base", "finalize_fda_molecules"],
            parallelism="serial",
            aca_job_name="med-enrich-drugs",
        ),
        StepSpec(
            # [ENRICH] Part B procedure coverage. Outside the three-collection
            # scope (LLD v83 III.1); best-effort -- never fails the run.
            name="enrich_procedures",
            prerequisites=["build_providermedicare_base"],
            parallelism="process_pool",
            aca_job_name="med-enrich-procedures",
            partition_key="provider_state",
        ),
        StepSpec(
            # [ENRICH] Organizational CCN + coarse size. Outside scope;
            # best-effort.
            name="enrich_org_ccn_size",
            prerequisites=["build_providermedicare_base"],
            parallelism="process_pool",
            aca_job_name="med-enrich-org-ccn-size",
            partition_key="provider_state",
        ),
        StepSpec(
            # [DERIVE] Serial staging-sourced NUCC rollup (LLD v83 III.10):
            # specialty_total vs de-duplicated molecule_real_total,
            # prescriber_count, and the max/min/median/avg statistics of every
            # quantitative measure (III.4.3) computed as the final pass;
            # provider_count from Provider_v_5.
            name="rollup_specialtymedicare",
            prerequisites=["enrich_drugs", "enrich_procedures", "enrich_org_ccn_size"],
            parallelism="process_pool",
            aca_job_name="med-rollup-specialtymedicare",
            partition_key="nucc_code",
        ),
        StepSpec(
            # The III.6 conformance checks with the III.7 hard-fail semantics,
            # version-scoped.
            name="medicare_contract_conformance",
            prerequisites=["rollup_specialtymedicare"],
            parallelism="serial",
            aca_job_name="med-contract-conformance",
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
        "build_fda_label_resolution": ["staging:openfda_labels"],
        # build mode consumes the per-label transient + the RxNorm vocabulary;
        # consume mode reads the published molecule collection instead.
        "finalize_fda_molecules": ["if_build_fda:transient",
                                   "if_build_fda:staging:rxnorm",
                                   "if_consume_fda:fda_published"],
        "prepare_molecule_masters": ["staging:medicare_partd", "provider_spine"],
        "build_providermedicare_base": ["staging:medicare_partd", "masters"],
        "enrich_drugs": ["masters", "fda_published"],
        "enrich_procedures": ["masters", "staging:medicare_coverage_hcpc",
                              "staging:medicare_coverage_icd10"],
        "enrich_org_ccn_size": ["masters", "staging:medicare_pos"],
        "rollup_specialtymedicare": ["staging:medicare_partd"],
        "medicare_contract_conformance": ["staging:medicare_partd", "masters",
                                          "specialty_published", "fda_published",
                                          "if_build_fda:staging:openfda_labels"],
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
                db_name, coll_name = ctx.provider_collection.split(".", 1)
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
        return enums

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
