# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Medicare Provider Evaluation Data Pipeline (EPIC-010-F-007) — orchestrator.

Declares PIPELINE_NAME = "medicare" and the StepSpec DAG realized from the
approved LLD Part III (medicare_detailed_sequence). Built on the generic run
lifecycle (EPIC-010-F-001); only the Medicare-specific steps and fan-out keys
live here. The fda_drug build (the fdaProductToDiagnosisResolution collection)
is a gated step on its own cadence (the build_fda_drug arg, carried as the
BUILD_FDA_DRUG env the step reads); the per-provider enrich steps are collection
lookups only; SpecialtyMedicare is a derive rollup over NUCC codes.
"""

from __future__ import annotations

import os

from pipeline.run_lifecycle.base_pipeline_orchestrator import BasePipelineOrchestrator
from pipeline.run_lifecycle.step_spec import StepSpec


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
            # [ENRICH] Index this run's prescription FDA labels (non-OTC) with a
            # sequential fda_record_number so the build can range-partition the FDA
            # work across worker processes (LLD I.3.4a). Serial, once.
            name="index_fda_prescription_records",
            prerequisites=["medicare_load_staging"],
            parallelism="serial",
            aca_job_name="med-index-fda-records",
        ),
        StepSpec(
            # [ENRICH] Build the fdaProductToDiagnosisResolution collection (the FDA
            # drug collection). Gated by the build_fda_drug arg: a normal run consumes
            # the current published collection; a rebuild produces a candidate released
            # through the operator-gated crossing (LLD I.6). Fanned out over record-
            # number ranges -- one worker PROCESS per range; concurrency is the worker-
            # process count, sized to the worker box.
            name="build_fda_drug_collection",
            prerequisites=["index_fda_prescription_records"],
            parallelism="process_pool",
            aca_job_name="med-build-fda-drug-collection",
            partition_key="record_range",
        ),
        StepSpec(
            # [BASE] Per-NPI ProviderMedicare record, fanned out by provider
            # state.
            name="build_providermedicare_base",
            prerequisites=["medicare_load_staging"],
            parallelism="process_pool",
            aca_job_name="med-build-providermedicare",
            partition_key="provider_state",
        ),
        StepSpec(
            # [ENRICH] Map lookup only: attribute each drug's day supply to the
            # drug's indication(s), count-under-each.
            name="enrich_drugs",
            prerequisites=["build_providermedicare_base", "build_fda_drug_collection"],
            parallelism="process_pool",
            aca_job_name="med-enrich-drugs",
            partition_key="provider_state",
        ),
        StepSpec(
            # [ENRICH] Map lookup only: attach each procedure's allowed ICD-10
            # to its indication(s).
            name="enrich_procedures",
            prerequisites=["build_providermedicare_base", "build_fda_drug_collection"],
            parallelism="process_pool",
            aca_job_name="med-enrich-procedures",
            partition_key="provider_state",
        ),
        StepSpec(
            # [ENRICH] Organizations only: CCN + coarse size_tier from the
            # enrollment and POS sources.
            name="enrich_org_ccn_size",
            prerequisites=["build_providermedicare_base"],
            parallelism="process_pool",
            aca_job_name="med-enrich-org-ccn-size",
            partition_key="provider_state",
        ),
        StepSpec(
            # [DERIVE] Roll up SpecialtyMedicare per NUCC: procedure/drug/
            # indication aggregates plus the day-supply mean, median and
            # quintile cut-points per drug and per indication; provider_count
            # is read from the provider base (any taxonomy slot); the document
            # is embedded and indexed. A rollup plus an embedding.
            name="rollup_specialtymedicare",
            prerequisites=["enrich_drugs", "enrich_procedures", "enrich_org_ccn_size"],
            parallelism="process_pool",
            aca_job_name="med-rollup-specialtymedicare",
            partition_key="nucc_code",
        ),
        StepSpec(
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
    # This override is additive: it resolves the Medicare source partitions and
    # the provider_state / nucc_code keys, delegating every other key to the
    # base. The fully declarative per-step partition source is BUG-014; this is
    # the interim, backward-compatible shape and does not alter the base.
    # Fetchable sources declared in pipeline_config dataset_versions[]. The
    # indication-map sources that still need loader/format support (the Medicare
    # Coverage Database, FDA labels, the UMLS crosswalks) and the organization
    # sources (hospital enrollments, POS) join this list as they are declared
    # and the loader gains their formats.
    # Sources that own a download (fetched). The two Coverage DB tables (HCPCS
    # and ICD-10) each fetch the same Coverage DB archive and extract their own
    # member from it (fetch.extract, a cascading-zip member path), so each is an
    # independent fetched source -- there is no cross-source bundle extraction.
    FETCH_SOURCES = [
        "medicare_partb",
        "medicare_partd",
        "ccsr",
        "icd10_cm",
        "openfda_labels",
        "medicare_pos",
        "medicare_coverage_hcpc",
        "medicare_coverage_icd10",
    ]
    # Every fetched source also lands in staging; nothing is load-only now.
    LOAD_SOURCES = FETCH_SOURCES

    def _partition_enumerators(self):
        # The fan-out dimension and worker cap for EVERY step are declared in
        # persistent config (fan_out.medicare.<step>) per §I.3.4 -- there is no
        # _partitions_for override here. This method only supplies the one code
        # hook the config names: the Medicare record-range enumerator, merged
        # onto the base geographic enumerators (state, county, state_entity).
        enums = dict(super()._partition_enumerators())
        enums["record_ranges"] = self._fda_record_ranges
        return enums

    def _fda_record_ranges(self, ctx, **_) -> list[dict]:
        """Even record-number ranges over this run's stamped prescription FDA labels --
        one worker PROCESS per range (LLD I.3.4a). Off-build returns a single
        verify/consume partition. Range size is configurable per pipeline via
        fda_drug_records_per_partition."""
        if os.environ.get("BUILD_FDA_DRUG", "").strip().lower() not in ("1", "true", "yes"):
            return [{"single": True}]
        from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
        reg = PipelineRuntime(ctx).registry
        ofda = reg.by_source_name("openfda_labels")
        staging = ctx.mongo_client[ofda.staging_db][reg.staging_collection_name("openfda_labels")]
        total = staging.count_documents(
            {"run_id": ctx.run_id, "fda_record_number": {"$exists": True}})
        if total <= 0:
            return [{"single": True}]
        worker_count = max(1, min(int((ctx.config or {}).get("fda_drug_worker_count", 100)),
                                  total))
        size = -(-total // worker_count)  # ceil: ~worker_count even ranges, one per process
        return [{"record_range": {"start": s, "end": min(s + size, total + 1)}}
                for s in range(1, total + 1, size)]
