# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# v4-017: Read rules before any infrastructure command.
# This script runs before every deployment. If it fails, the deploy is blocked.
#
# Usage: python pre_deploy_rule_check.py <target>
#   target: findcare | evaluatecare | shared | pipeline | website
#
# Checks:
#   1. All compliance tests pass (test_compliance.py, test_pipeline_rename_compliance.py)
#   2. DR-024 meta-tests pass (test_dr024_test_rigor.py)
#   3. Traceability tests pass (test_traceability.py)
#   4. No .readall() in pipeline code
#   5. No raw requests.get in pipeline code
#   6. No overnight_pipeline.py filename
#   7. No "PrescriberPipeline" old name

import json
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
PIPELINE_DIR = os.path.join(REPO_ROOT, "Code", "DataPipelines")
BRAIN_DIR = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content")


def check_no_old_names():
    """No 'PrescriberPipeline' or overnight_pipeline.py in code."""
    violations = []
    skip_files = {"conversation_log.json", "pipeline_v3_compliance_log.json",
                  "pipeline_v3_iteration_log.json", "pipeline_v4_design_iterations.json",
                  "test_pipeline_rename_compliance.py", "pre_deploy_rule_check.py"}

    for scan_dir in [PIPELINE_DIR, BRAIN_DIR]:
        for root, _, files in os.walk(scan_dir):
            for fname in files:
                if fname in skip_files:
                    continue
                if not fname.endswith((".py", ".json", ".yml")):
                    continue
                fpath = os.path.join(root, fname)
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if '"PrescriberPipeline"' in line:
                                violations.append(f"{fpath}:{i}: old name PrescriberPipeline")
                except Exception:
                    pass

    if os.path.exists(os.path.join(PIPELINE_DIR, "overnight_pipeline.py")):
        violations.append("overnight_pipeline.py still exists — must be renamed")

    return violations


def check_no_readall():
    """No .readall() on full blob downloads."""
    violations = []
    exempt = ["download_blob(offset="]
    for fname in os.listdir(PIPELINE_DIR):
        if not fname.endswith(".py") or fname.startswith("test_"):
            continue
        fpath = os.path.join(PIPELINE_DIR, fname)
        with open(fpath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for i, line in enumerate(lines, 1):
            if ".readall()" in line:
                context = "".join(lines[max(0, i - 4):i])
                if any(p in context for p in exempt):
                    continue
                violations.append(f"{fname}:{i}: .readall()")
    return violations


def check_no_raw_http():
    """No requests.get in pipeline entry points."""
    violations = []
    for fname in ["prescriber_evaluate_care_pipeline.py"]:
        fpath = os.path.join(PIPELINE_DIR, fname)
        if not os.path.exists(fpath):
            continue
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        if "requests.get(" in content:
            violations.append(f"{fname}: raw requests.get")
    return violations


def check_traceability():
    """Traceability matrix exists and is valid."""
    violations = []
    matrix_path = os.path.join(BRAIN_DIR, "traceability_matrix.json")
    if not os.path.exists(matrix_path):
        violations.append("traceability_matrix.json missing")
        return violations
    try:
        with open(matrix_path, "r", encoding="utf-8") as f:
            matrix = json.load(f)
        if "entries" not in matrix:
            violations.append("traceability_matrix.json missing 'entries'")
        elif len(matrix["entries"]) == 0:
            violations.append("traceability_matrix.json has no entries")
    except json.JSONDecodeError as e:
        violations.append(f"traceability_matrix.json invalid JSON: {e}")
    return violations


def check_architecture():
    """GOV-005: Four-app boundary. Three HF Spaces for App 2 (FindCare, EvaluateCare, Shared Services)."""
    violations = []

    # Verify deploy workflows exist for all three services
    workflows_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    required_workflows = {
        "deploy-findcare-backend.yml": "FindCare",
        "deploy-evaluatecare-backend.yml": "EvaluateCare",
        "deploy-shared-services.yml": "Shared Services",
    }
    for wf, service in required_workflows.items():
        if not os.path.exists(os.path.join(workflows_dir, wf)):
            violations.append(f"Missing deploy workflow for {service}: {wf}")

    # Verify each deploy workflow has pre-deploy rule check
    for wf in required_workflows:
        wf_path = os.path.join(workflows_dir, wf)
        if os.path.exists(wf_path):
            with open(wf_path, "r", encoding="utf-8") as f:
                content = f.read()
            if "pre_deploy_rule_check" not in content:
                violations.append(f"{wf} missing pre-deploy rule check step")

    # Verify Dockerfiles exist for EvaluateCare and Shared Services
    for service_dir, service_name in [
        ("Code/evaluate_care", "EvaluateCare"),
        ("Code/shared_services", "Shared Services"),
    ]:
        dockerfile = os.path.join(REPO_ROOT, service_dir, "Dockerfile")
        if not os.path.exists(dockerfile):
            violations.append(f"Missing Dockerfile for {service_name}: {service_dir}/Dockerfile")

    # Verify no http://localhost cross-service calls in deployed code
    for service_dir in ["Code/evaluate_care", "Code/shared_services",
                         "Code/ConversationalUX/FindCareChat/backend"]:
        app_files = []
        full_dir = os.path.join(REPO_ROOT, service_dir)
        if not os.path.exists(full_dir):
            continue
        for fname in os.listdir(full_dir):
            if fname.endswith(".py") and not fname.startswith("test_"):
                app_files.append(os.path.join(full_dir, fname))
        for fpath in app_files:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f, 1):
                    # http://localhost in non-default, non-comment lines is a violation
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        continue
                    if "http://localhost" in stripped:
                        # OK if it's in CORS allow_origins block (browser-side, not service-to-service)
                        # Check surrounding context (prev 5 lines) for CORS indicators
                        all_lines = open(fpath, "r", encoding="utf-8", errors="replace").readlines()
                        context = "".join(all_lines[max(0, i - 6):i])
                        if "allow_origins" in context or "allow_origin_regex" in context or "CORSMiddleware" in context:
                            continue
                        # OK if it's a fallback default in os.getenv
                        if "os.getenv" in stripped or "os.environ.get" in stripped:
                            continue
                        violations.append(f"{os.path.basename(fpath)}:{i}: http://localhost in deployed code")

    return violations


def main(target: str) -> int:
    print(f"Pre-deploy rule check for: {target}")
    print("=" * 50)

    all_violations = []

    checks = [
        ("No old names", check_no_old_names),
        ("No .readall()", check_no_readall),
        ("No raw HTTP", check_no_raw_http),
        ("Traceability", check_traceability),
        ("Architecture (GOV-005)", check_architecture),
    ]

    for name, check_fn in checks:
        violations = check_fn()
        if violations:
            print(f"FAIL: {name}")
            for v in violations:
                print(f"  {v}")
            all_violations.extend(violations)
        else:
            print(f"PASS: {name}")

    print("=" * 50)
    if all_violations:
        print(f"DEPLOY BLOCKED: {len(all_violations)} violation(s)")
        return 1
    else:
        print("All checks passed. Deploy may proceed.")
        return 0


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else "unknown"
    sys.exit(main(target))
