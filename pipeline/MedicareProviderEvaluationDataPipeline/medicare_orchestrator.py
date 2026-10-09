# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Medicare Provider Evaluation Data Pipeline (EPIC-010-F-007) — orchestrator.

Declares PIPELINE_NAME = "medicare" and the StepSpec DAG realized from the
approved LLD Part III (medicare_detailed_sequence). Built on the generic run
lifecycle (EPIC-010-F-001); only the Medicare-specific steps and fan-out keys
live here. The indication-map build is a gated step on its own cadence (the
build_indication_map arg, carried as the BUILD_INDICATION_MAP env the step
reads); the per-provider enrich steps are map lookups only; SpecialtyMedicare
is a derive rollup over NUCC codes.

Slice 1 scaffold: step runners are registry-resolved stubs; the deterministic
mining joins, the rollups, and the thin LLM residual (behind the shared llm
facade) land in later slices.
"""

from __future__ import annotations

from pipeline.run_lifecycle.base_pipeline_orchestrator import BasePipelineOrchestrator
from pipeline.run_lifecycle.step_spec import StepSpec
from pipeline.run_lifecycle.steps._partitions import state_partitions


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
            # [ENRICH] Mine the Normalized Indication Map once. Own cadence,
            # gated by the build_indication_map arg: a normal run consumes the
            # current published map; a rebuild run produces a candidate released
            # through the operator-gated crossing (LLD I.6).
            name="build_indication_map",
            prerequisites=["medicare_load_staging"],
            parallelism="serial",
            aca_job_name="med-build-indication-map",
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
            prerequisites=["build_providermedicare_base", "build_indication_map"],
            parallelism="process_pool",
            aca_job_name="med-enrich-drugs",
            partition_key="provider_state",
        ),
        StepSpec(
            # [ENRICH] Map lookup only: attach each procedure's allowed ICD-10
            # to its indication(s).
            name="enrich_procedures",
            prerequisites=["build_providermedicare_base", "build_indication_map"],
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
    # Sources that own a download (fetched). The Coverage DB ICD-10 table is a
    # bundled extractor pulled from the HCPCS owner's zip at fetch, so it is NOT
    # fetched on its own -- only loaded.
    FETCH_SOURCES = [
        "medicare_partb",
        "medicare_partd",
        "ccsr",
        "icd10_cm",
        "openfda_labels",
        "medicare_pos",
        "medicare_coverage_hcpc",
    ]
    # Every source that lands in staging (owners + the bundled extractor).
    LOAD_SOURCES = FETCH_SOURCES + ["medicare_coverage_icd10"]

    def _partitions_for(self, spec, ctx):
        if spec.parallelism in (None, "serial", "gather"):
            return [{"single": True}]
        key = spec.partition_key
        if key == "source_name_base":
            return [{"source": s} for s in self.FETCH_SOURCES]
        if key == "source_name":
            return [{"source": s} for s in self.LOAD_SOURCES]
        if key == "provider_state":
            # Reuse the generic 52-way state fan-out logic, re-keyed to
            # provider_state so a ProviderMedicare worker owns one state.
            return [
                {"provider_state": part["business_address_state"]}
                for part in state_partitions(ctx.args.partition_states())
            ]
        if key == "nucc_code":
            # Slice 2: fan out over the distinct nucc_code values present in the
            # built ProviderMedicare collection (a declared partition source).
            # The skeleton runs a single partition until that query lands.
            return [{"single": True}]
        return super()._partitions_for(spec, ctx)
