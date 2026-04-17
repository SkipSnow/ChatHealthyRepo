#!/usr/bin/env bash
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# deploy_app_dev.sh — Full ChatHealthy.ai app deploy to dev
#   Website  -> Cloudflare Pages (chathealthy-website-dev)
#   FindCare -> HuggingFace Space (SkipSnow/dev_ChatHealthySpace)
#   EvalCare -> HuggingFace Space (SkipSnow/dev_EvaluateCareSpace)
#   Shared   -> HuggingFace Space (SkipSnow/dev_SharedServicesSpace)
#
# Implements requirements (BRAIN-CONVERSATION-LOG traceability):
#   DEVOPS-DEPLOY-001-REQ-002 (dev deploy script exists)
#   DEVOPS-DEPLOY-001-REQ-006 (zombie-process check — remote-adapted)
#   DEVOPS-DEPLOY-001-REQ-007 (current-branch code is what deploys)
#   DEVOPS-DEPLOY-001-REQ-009 (verify all servers healthy post-deploy)
#   DEVOPS-DEPLOY-001-REQ-011 (build React — delegated to CI workflow)
#   DEVOPS-DEV-B001          (standard ports: 80/443/7860/8001/8002)
#   DEVOPS-DEV-B002          (Playwright 31-step smoke — GAP: localSmokeTestPyTest.py
#                             hardcodes https://localhost:7860/8001/8002 and is not
#                             parameterized for dev URLs; opt-in with --run-smoke, will
#                             fail until the smoke test is made environment-aware.)
#   DEVOPS-DEV-B003          (structured output JSON to test_output/deploy/)
#   DEVOPS-DEV-B004          (log state of each endpoint before + after)
#
# Usage:
#   bash Code/deploy/deploy_app_dev.sh               # push + wait + verify (smoke skipped)
#   bash Code/deploy/deploy_app_dev.sh --run-smoke   # also run Playwright smoke (currently broken vs dev)
#   bash Code/deploy/deploy_app_dev.sh --verify-only # no push; just probe endpoint state

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# .env not sourced: git and gh use their own auth on Boss's machine; curl hits public endpoints.
# If we later need env-driven values (HF_TOKEN for direct HF API calls, etc.), load them
# selectively via python rather than sourcing the whole file (the file contains comment-like
# lines that break `source`).

HF_USER="SkipSnow"
BRANCH="dev"

WEBSITE_URL="https://chathealthy-website-dev.pages.dev"
FINDCARE_URL="https://skipsnow-dev-chathealthyspace.hf.space"
EVALCARE_URL="https://skipsnow-dev-evaluatecarespace.hf.space"
SHARED_URL="https://skipsnow-dev-sharedservicesspace.hf.space"

TS=$(date -u +%Y%m%dT%H%M%SZ)
OUT_DIR="test_output/deploy"
OUT_JSON="$OUT_DIR/dev-$TS.json"
mkdir -p "$OUT_DIR"

RUN_SMOKE=0
VERIFY_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --run-smoke) RUN_SMOKE=1 ;;
        --verify-only) VERIFY_ONLY=1 ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done
SKIP_SMOKE=$((1 - RUN_SMOKE))

# ── Output state (accumulated across phases, emitted as JSON at end) ──────

declare -A STATE
STATE[phase_current]="init"
STATE[start_ts]="$TS"
STATE[branch]="$BRANCH"
STATE[skip_smoke]="$SKIP_SMOKE"
STATE[verify_only]="$VERIFY_ONLY"
STATE[website_before]=""
STATE[website_after]=""
STATE[findcare_before]=""
STATE[findcare_after]=""
STATE[evalcare_before]=""
STATE[evalcare_after]=""
STATE[shared_before]=""
STATE[shared_after]=""
STATE[old_build]=""
STATE[new_build]=""
STATE[workflows_completed]=""
STATE[workflows_failed]=""
STATE[smoke_status]="not_run"
STATE[overall]="pending"
STATE[exit_code]="0"

log() { printf "[%s] %s\n" "$(date +%H:%M:%S)" "$*"; }

# ── Helpers ───────────────────────────────────────────────────────────────

probe_endpoint() {
    local url="$1"
    local code
    code=$(curl -s -o /dev/null -w "%{http_code}" -m 10 "$url" 2>/dev/null || echo "000")
    echo "$code"
}

probe_findcare_build() {
    curl -s -m 10 "$FINDCARE_URL/health" 2>/dev/null \
        | python -c "import sys,json; print(json.load(sys.stdin).get('build','?'))" 2>/dev/null \
        || echo "?"
}

write_output_json() {
    python - <<PY >"$OUT_JSON"
import json
state = {
    "collection": "deploy_run",
    "env": "dev",
    "start_ts": "${STATE[start_ts]}",
    "branch": "${STATE[branch]}",
    "skip_smoke": ${STATE[skip_smoke]},
    "verify_only": ${STATE[verify_only]},
    "endpoints_before": {
        "website":   "${STATE[website_before]}",
        "findcare":  "${STATE[findcare_before]}",
        "evalcare":  "${STATE[evalcare_before]}",
        "shared":    "${STATE[shared_before]}",
    },
    "endpoints_after": {
        "website":   "${STATE[website_after]}",
        "findcare":  "${STATE[findcare_after]}",
        "evalcare":  "${STATE[evalcare_after]}",
        "shared":    "${STATE[shared_after]}",
    },
    "findcare_build_before": "${STATE[old_build]}",
    "findcare_build_after":  "${STATE[new_build]}",
    "workflows_completed":   "${STATE[workflows_completed]}",
    "workflows_failed":      "${STATE[workflows_failed]}",
    "smoke_status":          "${STATE[smoke_status]}",
    "overall":               "${STATE[overall]}",
    "exit_code":             ${STATE[exit_code]},
    "phase_current":         "${STATE[phase_current]}",
}
print(json.dumps(state, indent=2))
PY
}

finish() {
    STATE[exit_code]="$?"
    write_output_json
    log "Output written: $OUT_JSON"
}
trap finish EXIT

# ── Phase 1: Pre-flight ───────────────────────────────────────────────────

STATE[phase_current]="preflight"
log "=========================================="
log "  ChatHealthy.ai App Deploy to DEV"
log "  Website + FindCare + EvaluateCare + Shared"
log "=========================================="

# DEVOPS-DEPLOY-001-REQ-007: current branch is dev
current_branch=$(git rev-parse --abbrev-ref HEAD)
if [ "$current_branch" != "$BRANCH" ]; then
    log "FAIL: must be on '$BRANCH' branch. Currently: $current_branch"
    STATE[overall]="preflight_branch_fail"
    exit 1
fi
log "Branch: $current_branch [OK]"

# Uncommitted-changes warning (DEVOPS-DEPLOY-001-REQ-007 — cannot deploy what isn't committed)
if [ -n "$(git status --porcelain)" ]; then
    log "WARNING: Uncommitted changes present. These will NOT deploy unless you commit first."
    git status --short
    if [ "$VERIFY_ONLY" -eq 0 ]; then
        read -rp "Continue anyway (deploying only what's committed)? (y/N) " -n 1 ans
        echo
        if [[ ! "$ans" =~ ^[Yy]$ ]]; then
            STATE[overall]="preflight_user_abort"
            exit 1
        fi
    fi
fi

# Remote tracking
git fetch origin "$BRANCH" --quiet 2>/dev/null || true

# ── Phase 2: state before (DEVOPS-DEV-B004) ───────────────────────────────

STATE[phase_current]="state_before"
log "Probing endpoints (before)..."
STATE[website_before]=$(probe_endpoint "$WEBSITE_URL/")
STATE[findcare_before]=$(probe_endpoint "$FINDCARE_URL/health")
STATE[evalcare_before]=$(probe_endpoint "$EVALCARE_URL/health")
STATE[shared_before]=$(probe_endpoint "$SHARED_URL/health")
STATE[old_build]=$(probe_findcare_build)
log "  website:   ${STATE[website_before]}  $WEBSITE_URL"
log "  findcare:  ${STATE[findcare_before]}  $FINDCARE_URL (build ${STATE[old_build]})"
log "  evalcare:  ${STATE[evalcare_before]}"
log "  shared:    ${STATE[shared_before]}"

if [ "$VERIFY_ONLY" -eq 1 ]; then
    STATE[phase_current]="verify_only_done"
    STATE[overall]="verify_only_ok"
    log "--verify-only: stopping after state probe."
    exit 0
fi

# ── Phase 3: Pre-deploy rule check (v4-017 / DEVOPS-001-REQ-007) ─────────

STATE[phase_current]="rule_check"
log "Pre-deploy rule check (all targets)..."
for target in findcare evaluatecare website shared; do
    log "  ...$target"
    python Code/Shared/ops/tools/pre_deploy_rule_check.py "$target" \
        || { log "FAIL: pre_deploy_rule_check failed for $target"; STATE[overall]="rule_check_fail"; exit 1; }
done
log "Pre-deploy rule check passed."

# ── Phase 4: Push to dev ─────────────────────────────────────────────────

STATE[phase_current]="push"
log "Pushing to origin/$BRANCH..."

push_ts=$(date -u +%Y-%m-%dT%H:%M:%SZ)
if ! git push origin "$BRANCH"; then
    log "FAIL: git push errored."
    STATE[overall]="push_fail"
    exit 1
fi
log "Pushed."

# ── Phase 5: Wait for all triggered workflows ─────────────────────────────
# DEVOPS-DEPLOY-001-REQ-009

STATE[phase_current]="workflows_wait"
log "Waiting for GitHub Actions workflows triggered by the push..."
log "  (push_ts=$push_ts — checking any run created >= this)"

# Give GH a few seconds to register
sleep 6

deadline=$(( $(date +%s) + 1500 ))   # 25-minute overall cap
completed_runs=""
failed_runs=""

while :; do
    now=$(date +%s)
    if [ $now -gt $deadline ]; then
        log "TIMEOUT: workflows did not all complete within 25 minutes."
        STATE[overall]="workflow_timeout"
        exit 1
    fi

    # Recent runs on dev since the push
    runs_json=$(gh run list --branch "$BRANCH" --limit 20 \
                --json databaseId,status,conclusion,name,createdAt,event 2>/dev/null || echo "[]")

    mapfile -t pending < <(python - <<PY
import json, sys
from datetime import datetime, timezone
push_ts = "$push_ts"
push_dt = datetime.strptime(push_ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
runs = json.loads('''$runs_json''' or '[]')
for r in runs:
    try:
        created = datetime.strptime(r["createdAt"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        continue
    if created < push_dt:
        continue
    # Only care about push-event runs (workflow_dispatch manual runs handled separately)
    if r.get("event") != "push":
        continue
    status = r.get("status","")
    concl  = r.get("conclusion") or ""
    if status != "completed":
        print(f"PENDING|{r['databaseId']}|{status}|{r['name']}")
    else:
        print(f"DONE|{r['databaseId']}|{concl}|{r['name']}")
PY
)

    pending_count=0
    for line in "${pending[@]}"; do
        [ -z "$line" ] && continue
        state=$(echo "$line" | cut -d'|' -f1)
        rid=$(echo "$line" | cut -d'|' -f2)
        concl=$(echo "$line" | cut -d'|' -f3)
        name=$(echo "$line" | cut -d'|' -f4)
        if [ "$state" = "PENDING" ]; then
            pending_count=$((pending_count+1))
        else
            # DONE
            if ! echo "$completed_runs" | grep -q "$rid"; then
                completed_runs="$completed_runs $rid:$concl:$name"
                log "  [$concl] $name (run $rid)"
                if [ "$concl" != "success" ] && [ "$concl" != "skipped" ]; then
                    failed_runs="$failed_runs $rid:$concl:$name"
                fi
            fi
        fi
    done

    if [ $pending_count -eq 0 ]; then
        # Either all runs complete, or no runs were triggered (path filter didn't match)
        if [ -z "$completed_runs" ]; then
            log "NOTE: No workflows triggered by the push (path filters did not match any changed file)."
            log "      Dev deploy skipped for all components."
        fi
        break
    fi
    log "  $pending_count workflow(s) still running..."
    sleep 15
done

STATE[workflows_completed]="$(echo "$completed_runs" | xargs echo)"
STATE[workflows_failed]="$(echo "$failed_runs" | xargs echo)"

if [ -n "$failed_runs" ]; then
    log "FAIL: one or more workflows failed: $failed_runs"
    STATE[overall]="workflow_failed"
    exit 1
fi

# ── Phase 6: Endpoint verification (DEVOPS-001-REQ-009 / DEV-B004) ───────

STATE[phase_current]="verify"
log "Verifying endpoints (after)..."

# Give HF container time to switch over (CI step completes before container restart finishes)
sleep 20

STATE[website_after]=$(probe_endpoint "$WEBSITE_URL/")
STATE[findcare_after]=$(probe_endpoint "$FINDCARE_URL/health")
STATE[evalcare_after]=$(probe_endpoint "$EVALCARE_URL/health")
STATE[shared_after]=$(probe_endpoint "$SHARED_URL/health")
STATE[new_build]=$(probe_findcare_build)

log "  website:   ${STATE[website_after]}  $WEBSITE_URL"
log "  findcare:  ${STATE[findcare_after]}  $FINDCARE_URL (build ${STATE[old_build]} -> ${STATE[new_build]})"
log "  evalcare:  ${STATE[evalcare_after]}"
log "  shared:    ${STATE[shared_after]}"

verify_fail=0
for name in website findcare evalcare shared; do
    var="${name}_after"
    code=${STATE[$var]}
    if [ "$code" != "200" ]; then
        log "FAIL: $name returned $code (expected 200)"
        verify_fail=1
    fi
done

if [ "$verify_fail" -eq 1 ]; then
    STATE[overall]="endpoint_verify_fail"
    exit 1
fi

# ── Phase 7: Playwright smoke test (DEVOPS-DEV-B002) ─────────────────────

if [ "$SKIP_SMOKE" -eq 0 ]; then
    STATE[phase_current]="smoke"
    log "Running Playwright smoke test vs dev.chathealthy.ai..."
    log "NOTE: localSmokeTestPyTest.py currently hardcodes localhost URLs — will likely fail against dev until parameterized."
    if SMOKE_TEST_URL="https://dev.chathealthy.ai" \
           python -m pytest Code/deploy/localSmokeTestPyTest.py -v 2>&1 | tee "$OUT_DIR/dev-$TS-smoke.log"; then
        STATE[smoke_status]="passed"
        log "Smoke test: PASSED"
    else
        STATE[smoke_status]="failed"
        log "FAIL: Playwright smoke test failed. See $OUT_DIR/dev-$TS-smoke.log"
        STATE[overall]="smoke_fail"
        exit 1
    fi
else
    STATE[smoke_status]="skipped_default"
    log "Skipping Playwright smoke test (not opt-in with --run-smoke)."
fi

# ── Done ──────────────────────────────────────────────────────────────────

STATE[phase_current]="done"
STATE[overall]="success"
log "=========================================="
log "  ALL CHECKS PASSED"
log "  dev.chathealthy.ai + HF backends are live."
log "  FindCare build: ${STATE[old_build]} -> ${STATE[new_build]}"
log "=========================================="
exit 0
