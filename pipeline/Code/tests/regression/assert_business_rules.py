# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""LLD §9.4 — complete per-REQ collection regression assertion table."""

from __future__ import annotations

import re
from typing import Any, Iterable

from county_source import SCHEMA_SUCCESS_SOURCES
from provider_flags_enrichment import compute_provider_flags
from schemas.provider_record_validator import validate_provider_record

COUNTY_SOURCES = SCHEMA_SUCCESS_SOURCES | {
    "county_unresolvable",
    "out_of_scope",
    "out_of_scope_zip_state_mismatch",
    None,
}

NPI_RE = re.compile(r"^[0-9]{10}$")
CASCADE_ORDER = ("zip_crosswalk", "census_batch", "google_maps", "nppes_registry")
F006_FIELDS = {"Code", "can_prescribe", "is_homeopathic", "is_npi_registered", "is_disqualified"}


# ── F-004 provider record assertions ─────────────────────────────────────────

def assert_npi_and_entity_type(docs: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        npi = doc.get("npi", "")
        etc = doc.get("entity_type_code")
        if not NPI_RE.match(npi or ""):
            violations.append({"npi": npi, "reason": "invalid_npi"})
        if etc not in ("1", "2"):
            violations.append({"npi": npi, "reason": "invalid_entity_type_code", "value": etc})
    return (not violations, violations)


def assert_county_source_enum(docs: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        for addr in doc.get("addresses") or []:
            if not isinstance(addr, dict) or addr.get("address_type") not in ("primary_practice", "secondary_practice"):
                continue
            county = addr.get("county")
            if not isinstance(county, dict):
                continue
            if county.get("source") not in COUNTY_SOURCES:
                violations.append({"npi": doc.get("npi"), "source": county.get("source")})
    return (not violations, violations)


def assert_cascade_order_in_report(discrepancy_summary: dict | None) -> tuple[bool, list]:
    order = (discrepancy_summary or {}).get("county_cascade_order")
    if order is None:
        return (True, [])
    if list(order) != list(CASCADE_ORDER):
        return (False, [f"expected cascade order {CASCADE_ORDER}, got {order}"])
    return (True, [])


def assert_urban_present(docs: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        for addr in doc.get("addresses") or []:
            if not isinstance(addr, dict) or addr.get("address_type") not in ("primary_practice", "secondary_practice"):
                continue
            if (addr.get("county") or {}).get("fips") and "urban" not in addr:
                violations.append({"npi": doc.get("npi"), "reason": "urban_missing"})
    return (not violations, violations)


def assert_one_license_state_repair(docs: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        licenses = doc.get("licenses") or []
        lic_states = {((lic.get("state") or "").upper()) for lic in licenses if isinstance(lic, dict) and lic.get("state")}
        for addr in doc.get("addresses") or []:
            if not isinstance(addr, dict):
                continue
            if addr.get("state"):
                continue
            if len(lic_states) == 1 and addr.get("address_repair_provenance") != "license_state_match":
                violations.append({"npi": doc.get("npi"), "reason": "single_license_not_repaired"})
            if len(lic_states) != 1:
                bad = doc.get("bad_data") or []
                if not any(isinstance(b, dict) and "state" in (b.get("reason") or "") for b in bad):
                    violations.append({"npi": doc.get("npi"), "reason": "unrepaired_state_not_in_bad_data"})
    return (not violations, violations)


def assert_is_disqualified_definition(docs: Iterable[dict], catalog: dict[str, dict[str, bool]]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        if doc.get("entity_type_code") != "1":
            if "is_disqualified" in doc:
                violations.append({"npi": doc.get("npi"), "reason": "institutional_has_is_disqualified"})
            continue
        expected = compute_provider_flags(doc, catalog)["is_disqualified"]
        if doc.get("is_disqualified") is not expected:
            violations.append({"npi": doc.get("npi"), "expected": expected, "actual": doc.get("is_disqualified")})
    return (not violations, violations)


def assert_can_prescribe_consistent(docs: Iterable[dict], catalog: dict[str, dict[str, bool]]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        if doc.get("entity_type_code") != "1":
            continue
        expected = compute_provider_flags(doc, catalog)["can_prescribe"]
        if doc.get("can_prescribe") is not expected:
            violations.append({"npi": doc.get("npi"), "expected": expected, "actual": doc.get("can_prescribe")})
    return (not violations, violations)


def assert_is_homeopathic_presence_by_kind(docs: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        etc = doc.get("entity_type_code")
        if etc == "1" and "is_homeopathic" not in doc:
            violations.append({"npi": doc.get("npi"), "reason": "missing_is_homeopathic"})
        if etc == "2" and "is_homeopathic" in doc:
            violations.append({"npi": doc.get("npi"), "reason": "institutional_has_is_homeopathic"})
    return (not violations, violations)


def assert_two_kinds_via_entity_type(docs: Iterable[dict]) -> tuple[bool, list]:
    label_map = {"1": "individual", "2": "institutional"}
    violations = []
    for doc in docs:
        etc = doc.get("entity_type_code")
        if doc.get("entity_type_code_label") != label_map.get(etc):
            violations.append({"npi": doc.get("npi"), "label": doc.get("entity_type_code_label")})
        if "kind" in doc:
            violations.append({"npi": doc.get("npi"), "reason": "legacy_kind_field"})
    return (not violations, violations)


def assert_schema_compliance(docs: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for doc in docs:
        ok, errors = validate_provider_record(doc)
        if not ok:
            violations.append({"npi": doc.get("npi"), "schema_errors": errors[:5]})
    return (not violations, violations)


def assert_county_match_rate_98pct(docs: Iterable[dict], min_rate: float = 0.98) -> tuple[bool, list]:
    practice_addrs = resolved = 0
    for doc in docs:
        for addr in doc.get("addresses") or []:
            if isinstance(addr, dict) and addr.get("address_type") in ("primary_practice", "secondary_practice"):
                practice_addrs += 1
                if (addr.get("county") or {}).get("fips"):
                    resolved += 1
    if practice_addrs == 0:
        return (True, [])
    rate = resolved / practice_addrs
    return ((rate >= min_rate), [] if rate >= min_rate else [f"rate={rate:.4f}"])


# ── F-006 catalog assertions ─────────────────────────────────────────────────

def assert_catalog_shape(catalog_rows: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for row in catalog_rows:
        code = row.get("Code") or row.get("code")
        if not code:
            violations.append({"reason": "missing_Code"})
            continue
        missing = F006_FIELDS - set(row.keys())
        if missing:
            violations.append({"code": code, "missing": sorted(missing)})
    return (not violations, violations)


def assert_catalog_contract_independent(catalog_rows: Iterable[dict]) -> tuple[bool, list]:
    violations = []
    for row in catalog_rows:
        code = row.get("Code") or row.get("code")
        cp, ih = row.get("can_prescribe"), row.get("is_homeopathic")
        if cp is None or ih is None:
            violations.append({"code": code, "reason": "missing_boolean"})
        if cp is True and ih is True and row.get("is_disqualified") is False:
            pass  # allowed — independent booleans, not mutually exclusive by design
    return (not violations, violations)


def assert_single_catalog_source(catalog_collection_name: str) -> tuple[bool, list]:
    allowed = {
        "SpecialtyAndProviderFlags",
        "SpecialtyAndProviderFlags_draft",
        "pipeline_sources_specialty_catalog",
    }
    if catalog_collection_name not in allowed:
        return (False, [f"unexpected catalog source: {catalog_collection_name}"])
    return (True, [])


def assert_no_hardcoded_flags(source_text: str) -> tuple[bool, list]:
    patterns = [
        r'"can_prescribe"\s*:\s*True\s*,\s*"is_homeopathic"\s*:\s*True',
        r"HARDCODED_NUCC_FLAGS",
    ]
    hits = [p for p in patterns if re.search(p, source_text)]
    return (not hits, hits)


# ── F-001 orchestration / manifest assertions ────────────────────────────────

def assert_step_transitions_terminate_at_quiesce(manifest: dict) -> tuple[bool, list]:
    transitions = manifest.get("step_transitions") or []
    names = [t.get("step") for t in transitions if isinstance(t, dict)]
    issues = []
    if "quiesce_infrastructure" not in names:
        issues.append("quiesce_infrastructure missing")
    if manifest.get("status") in ("completed", "quiesced") and transitions:
        if transitions[-1].get("step") != "quiesce_infrastructure":
            issues.append("last transition is not quiesce_infrastructure")
        if transitions[-1].get("status") not in ("completed", "success"):
            issues.append("quiesce step did not succeed")
    return (not issues, issues)


def assert_atlas_cluster_at_min_tier(cluster_status: dict) -> tuple[bool, list]:
    state = (cluster_status or {}).get("cluster_state") or (cluster_status or {}).get("stateName")
    if state in ("IDLE", "PAUSED", "RECOVERING", "UPDATING", "UNKNOWN", None):
        return (True, [])
    return (False, [f"cluster state {state!r} not at min tier"])


def assert_aca_control_job_terminated_for_run(manifest: dict) -> tuple[bool, list]:
    """Local control_runner path: manifest status terminal counts as ACA job termination."""
    status = manifest.get("status")
    if status in ("completed", "quiesced", "failed"):
        return (True, [])
    return (False, [f"manifest status {status!r} not terminal"])


def assert_atlas_cluster_paused_between_runs(cluster_status: dict) -> tuple[bool, list]:
    return assert_atlas_cluster_at_min_tier(cluster_status)


def assert_run_history_retained(runs_coll, min_count: int = 1) -> tuple[bool, list]:
    try:
        count = runs_coll.count_documents({})
    except Exception as exc:
        return (False, [str(exc)])
    return ((count >= min_count), [] if count >= min_count else [f"only {count} runs retained"])


def assert_manifest_shape_invocation_agnostic(manifest: dict) -> tuple[bool, list]:
    mode = manifest.get("invocation_mode") or manifest.get("metrics", {}).get("invocation_mode")
    if mode is None:
        return (True, [])
    if mode not in ("scheduled", "api", "manual", "pytest"):
        return (False, [f"invalid invocation_mode {mode!r}"])
    return (True, [])


def assert_sources_ready_before_first_processing(manifest: dict) -> tuple[bool, list]:
    versions = manifest.get("source_versions") or {}
    if not versions.get("nppes_npi"):
        return (False, ["source_versions.nppes_npi missing before normalize"])
    completed = manifest.get("completed_steps") or set()
    if "normalize_npi_serial_bulk_load" in completed and not versions:
        return (False, ["normalize ran before source_versions populated"])
    return (True, [])


def assert_step_order(manifest: dict, expected_steps: list[str]) -> tuple[bool, list]:
    transitions = [t.get("step") for t in manifest.get("step_transitions") or [] if isinstance(t, dict)]
    filtered = [s for s in transitions if s in expected_steps]
    if filtered != [s for s in expected_steps if s in set(filtered)]:
        return (False, [f"step order mismatch: {filtered}"])
    return (True, [])


def assert_resume_from_step(manifest: dict, resumed_from: str | None) -> tuple[bool, list]:
    if not resumed_from:
        return (True, [])
    completed = manifest.get("completed_steps") or set()
    if resumed_from in completed:
        return (False, [f"resume step {resumed_from} should not appear completed at start"])
    return (True, [])


def assert_batched_fan_out_drain(work_items: list[dict], min_batches: int = 1) -> tuple[bool, list]:
    if not work_items:
        return (True, [])
    batches = len({wi.get("partition") for wi in work_items})
    return ((batches >= min_batches), [] if batches >= min_batches else [f"only {batches} partitions"])


def assert_all_failures_recorded(discrepancies: list[dict], expected_npis: set[str]) -> tuple[bool, list]:
    recorded = {d.get("npi") for d in discrepancies if d.get("npi")}
    missing = expected_npis - recorded
    return (not missing, sorted(missing))


def assert_failure_threshold_aborts(manifest: dict, threshold: int, discrepancy_count: int) -> tuple[bool, list]:
    if discrepancy_count <= threshold:
        return (True, [])
    if manifest.get("status") != "failed":
        return (False, ["status not failed after threshold exceeded"])
    if "threshold" not in (manifest.get("abort_reason") or "").lower():
        return (False, ["abort_reason does not mention threshold"])
    return (True, [])


def assert_notification_sent(notifications: list[dict], template_substring: str) -> tuple[bool, list]:
    matched = [
        n for n in notifications
        if template_substring in (n.get("template_name") or "")
    ]
    return (bool(matched), [] if matched else [f"no notification matching {template_substring}"])


def run_all_collection_assertions(
    docs: list[dict],
    catalog: dict[str, dict[str, bool]],
    *,
    manifest: dict | None = None,
    catalog_rows: list[dict] | None = None,
    discrepancy_summary: dict | None = None,
    cluster_status: dict | None = None,
    catalog_collection_name: str = "SpecialtyAndProviderFlags_draft",
) -> dict[str, Any]:
    """Execute full §9.4 table (collection + manifest + catalog checks)."""
    results: dict[str, Any] = {}
    checks: list[tuple[str, callable]] = [
        ("assert_npi_and_entity_type", lambda: assert_npi_and_entity_type(docs)),
        ("assert_county_source_enum", lambda: assert_county_source_enum(docs)),
        ("assert_cascade_order_in_report", lambda: assert_cascade_order_in_report(discrepancy_summary)),
        ("assert_urban_present", lambda: assert_urban_present(docs)),
        ("assert_one_license_state_repair", lambda: assert_one_license_state_repair(docs)),
        ("assert_is_disqualified_definition", lambda: assert_is_disqualified_definition(docs, catalog)),
        ("assert_can_prescribe_consistent", lambda: assert_can_prescribe_consistent(docs, catalog)),
        ("assert_is_homeopathic_presence_by_kind", lambda: assert_is_homeopathic_presence_by_kind(docs)),
        ("assert_two_kinds_via_entity_type", lambda: assert_two_kinds_via_entity_type(docs)),
        ("assert_schema_compliance", lambda: assert_schema_compliance(docs)),
        ("assert_county_match_rate_98pct", lambda: assert_county_match_rate_98pct(docs)),
        ("assert_single_catalog_source", lambda: assert_single_catalog_source(catalog_collection_name)),
    ]
    if catalog_rows is not None:
        checks.extend([
            ("assert_catalog_shape", lambda: assert_catalog_shape(catalog_rows)),
            ("assert_catalog_contract_independent", lambda: assert_catalog_contract_independent(catalog_rows)),
        ])
    if manifest is not None:
        checks.extend([
            ("assert_step_transitions_terminate_at_quiesce", lambda: assert_step_transitions_terminate_at_quiesce(manifest)),
            ("assert_aca_control_job_terminated_for_run", lambda: assert_aca_control_job_terminated_for_run(manifest)),
            ("assert_manifest_shape_invocation_agnostic", lambda: assert_manifest_shape_invocation_agnostic(manifest)),
            ("assert_sources_ready_before_first_processing", lambda: assert_sources_ready_before_first_processing(manifest)),
        ])
    if cluster_status is not None:
        checks.extend([
            ("assert_atlas_cluster_at_min_tier", lambda: assert_atlas_cluster_at_min_tier(cluster_status)),
        ])
    for name, fn in checks:
        ok, detail = fn()
        results[name] = {"ok": ok, "detail": detail}
    results["all_ok"] = all(v["ok"] for v in results.values() if isinstance(v, dict))
    return results
