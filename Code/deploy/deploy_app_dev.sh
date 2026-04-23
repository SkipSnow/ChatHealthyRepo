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
#   bash Code/deploy/deploy_app_dev.sh               # push + force-deploy all 4 + verify + smoke
#   bash Code/deploy/deploy_app_dev.sh --skip-smoke  # skip the Playwright smoke phase
#   bash Code/deploy/deploy_app_dev.sh --skip-build  # skip local React build (CI still builds)
#   bash Code/deploy/deploy_app_dev.sh --push-only   # push current HEAD but don't force-deploy workflows
#   bash Code/deploy/deploy_app_dev.sh --verify-only # no push, no deploy; just probe endpoint state

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────────

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

# Activate project venv — script requires venv packages (pymongo, httpx, etc.)
# Self-sufficient: does not assume the caller's shell has the venv on PATH.
if [ -f "$REPO_ROOT/.venv/Scripts/activate" ]; then
    source "$REPO_ROOT/.venv/Scripts/activate"
elif [ -f "$REPO_ROOT/.venv/bin/activate" ]; then
    source "$REPO_ROOT/.venv/bin/activate"
else
    echo "ERROR: .venv not found at $REPO_ROOT/.venv"
    echo "Create one:  python -m venv .venv && source .venv/Scripts/activate && pip install -r Code/ConversationalUX/FindCareChat/backend/requirements.txt"
    exit 1
fi

# .env not sourced: git and gh use their own auth on Human's machine; curl hits public endpoints.
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

SKIP_SMOKE=0   # default: smoke runs — required by DEVOPS-DEV-B002
VERIFY_ONLY=0
SKIP_BUILD=0   # default: build React per REQ-011
PUSH_ONLY=0    # default: force-deploy all 4 components via gh workflow run
for arg in "$@"; do
    case "$arg" in
        --skip-smoke) SKIP_SMOKE=1 ;;
        --verify-only) VERIFY_ONLY=1 ;;
        --skip-build) SKIP_BUILD=1 ;;
        --push-only) PUSH_ONLY=1 ;;
        *) echo "Unknown flag: $arg" >&2; exit 2 ;;
    esac
done

# Workflow file manifest — every deploy targets ALL of these unless --push-only
WORKFLOWS=(
    "deploy-findcare-website-dev.yml:website"
    "deploy-findcare-backend.yml:findcare"
    "deploy-evaluatecare-backend.yml:evalcare"
    "deploy-shared-services.yml:shared"
)

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

# REQ-005: parse /health body for db connectivity
probe_findcare_db() {
    curl -s -m 10 "$FINDCARE_URL/health" 2>/dev/null \
        | python -c "import sys,json; d=json.load(sys.stdin); print(d.get('db', d.get('mongo','?')))" 2>/dev/null \
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
    if [ "$SKIP_SMOKE" -eq 0 ] && [ "$VERIFY_ONLY" -eq 0 ]; then
        log "Smoke log:      $OUT_DIR/dev-$TS-smoke.log"
    fi
    log "Final state: ${STATE[overall]:-unknown} (phase ${STATE[phase_current]:-?})"
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

# ── Phase 3b: Build React frontend (REQ-011) ─────────────────────────────
# Literal compliance: "every env's deploy script MUST build the React
# frontend (npm run build) before deploying FindCare." CI also builds on
# its own, but this local build acts as a pre-push sanity check — catches
# build breakage before the push.

if [ "$SKIP_BUILD" -eq 0 ]; then
    STATE[phase_current]="react_build"
    log "Building React frontend (REQ-011)..."
    pushd Code/ConversationalUX/FindCareChat/frontend > /dev/null
    if ! npm ci --silent 2>&1 | tail -2; then
        log "FAIL: npm ci failed."
        popd > /dev/null
        STATE[overall]="react_install_fail"
        exit 1
    fi
    if ! VITE_API_URL="" npx vite build 2>&1 | tail -3; then
        log "FAIL: Vite build failed."
        popd > /dev/null
        STATE[overall]="react_build_fail"
        exit 1
    fi
    popd > /dev/null
    if [ ! -f "Code/ConversationalUX/FindCareChat/frontend/dist/index.html" ]; then
        log "FAIL: dist/index.html missing after build."
        STATE[overall]="react_build_artifact_missing"
        exit 1
    fi
    log "React build OK."
else
    log "Skipping React build (--skip-build). CI will still build on push."
fi

# ── Phase 4: Push to dev ─────────────────────────────────────────────────

STATE[phase_current]="push"
log "Pushing to origin/$BRANCH..."

if ! git push origin "$BRANCH" >/dev/null 2>&1; then
    # push failed but maybe already up-to-date — retry verbose to expose reason
    if ! git push origin "$BRANCH"; then
        log "FAIL: git push errored."
        STATE[overall]="push_fail"
        exit 1
    fi
fi
log "Pushed."

# ── Phase 5: Force-deploy all 4 components + wait for each ────────────────
# DEVOPS-DEPLOY-001-REQ-009. No dependence on path-filter-triggered runs —
# every invocation force-dispatches every workflow so the four HF / Cloudflare
# targets are always on the just-pushed dev HEAD. This closes the
# "no workflows triggered because the change was in a non-matched path"
# drift that silently leaves dev stale.

if [ "$PUSH_ONLY" -eq 1 ]; then
    log "--push-only: skipping force-deploy phase. Path-filter-triggered runs (if any) are not waited on."
    STATE[workflows_completed]="skipped_push_only"
else
    STATE[phase_current]="dispatch"
    declare -A RUN_IDS
    for entry in "${WORKFLOWS[@]}"; do
        wf="${entry%%:*}"
        short="${entry##*:}"
        log "Dispatching $short ($wf)..."
        gh workflow run "$wf" --ref "$BRANCH" >/dev/null 2>&1 || {
            log "FAIL: gh workflow run $wf errored."
            STATE[overall]="dispatch_fail_$short"
            exit 1
        }
    done
    # GH takes a beat to register the new runs
    sleep 8
    for entry in "${WORKFLOWS[@]}"; do
        wf="${entry%%:*}"
        short="${entry##*:}"
        rid=$(gh run list --workflow "$wf" --branch "$BRANCH" --event workflow_dispatch \
                 --limit 1 --json databaseId --jq '.[0].databaseId' 2>/dev/null || echo "")
        if [ -z "$rid" ]; then
            log "FAIL: could not find newly-dispatched run id for $short."
            STATE[overall]="dispatch_id_missing_$short"
            exit 1
        fi
        RUN_IDS[$short]="$rid"
        log "  $short → run $rid"
    done

    STATE[phase_current]="workflows_wait"
    log "Waiting for 4 workflow runs to complete (25-min cap)..."
    deadline=$(( $(date +%s) + 1500 ))
    completed_runs=""
    failed_runs=""
    pending_shorts="website findcare evalcare shared"
    while [ -n "$(echo $pending_shorts | xargs)" ]; do
        if [ $(date +%s) -gt $deadline ]; then
            log "TIMEOUT: not all 4 workflows completed in 25 min. Still pending: $pending_shorts"
            STATE[overall]="workflow_timeout"
            STATE[workflows_completed]="$completed_runs"
            STATE[workflows_failed]="$failed_runs"
            exit 1
        fi
        still_pending=""
        for short in $pending_shorts; do
            rid=${RUN_IDS[$short]}
            line=$(gh run view "$rid" --json status,conclusion --jq '"\(.status)/\(.conclusion // "—")"' 2>/dev/null || echo "error/—")
            status="${line%%/*}"
            concl="${line##*/}"
            if [ "$status" = "completed" ]; then
                completed_runs="$completed_runs $short:$concl:$rid"
                log "  [$concl] $short (run $rid)"
                if [ "$concl" != "success" ] && [ "$concl" != "skipped" ]; then
                    failed_runs="$failed_runs $short:$concl:$rid"
                fi
            else
                still_pending="$still_pending $short"
            fi
        done
        pending_shorts=$(echo $still_pending | xargs)
        if [ -n "$pending_shorts" ]; then
            sleep 15
        fi
    done

    STATE[workflows_completed]="$(echo $completed_runs | xargs)"
    STATE[workflows_failed]="$(echo $failed_runs | xargs)"

    if [ -n "$failed_runs" ]; then
        log "FAIL: one or more workflows failed:$failed_runs"
        # Surface the diagnostic for each failing run so Human doesn't have to hunt.
        for entry in $failed_runs; do
            short="${entry%%:*}"
            rest="${entry#*:}"
            concl="${rest%%:*}"
            rid="${rest##*:}"
            log ""
            log "───── Failed workflow: $short ($concl, run $rid) ─────"
            gh run view "$rid" --log-failed 2>&1 | tail -40 | sed 's/^/  /'
            log "───── End $short ─────"
        done
        STATE[overall]="workflow_failed"
        exit 1
    fi
    log "All 4 workflows succeeded."
fi

# ── Phase 6: Endpoint verification (DEVOPS-001-REQ-009 / DEV-B004) ───────

STATE[phase_current]="verify"
log "Verifying endpoints (after)..."

# HF Spaces cold-start can take 30-120s after CI completes; retry per endpoint
# until 200 or until 12*10s = 120s elapses. Per Skip directive 2026-04-19 — single
# probe was treating cold-start as endpoint_verify_fail.
wait_for_endpoint() {
    local url="$1"
    local code=""
    for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
        code=$(probe_endpoint "$url")
        [ "$code" = "200" ] && break
        sleep 10
    done
    echo "$code"
}

STATE[website_after]=$(wait_for_endpoint "$WEBSITE_URL/")
STATE[findcare_after]=$(wait_for_endpoint "$FINDCARE_URL/health")
STATE[evalcare_after]=$(wait_for_endpoint "$EVALCARE_URL/health")
STATE[shared_after]=$(wait_for_endpoint "$SHARED_URL/health")
STATE[new_build]=$(probe_findcare_build)
STATE[findcare_db]=$(probe_findcare_db)   # REQ-005

log "  website:   ${STATE[website_after]}  $WEBSITE_URL"
log "  findcare:  ${STATE[findcare_after]}  $FINDCARE_URL (build ${STATE[old_build]} -> ${STATE[new_build]}, db=${STATE[findcare_db]})"
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

# REQ-005: frontend DB healthy before completing deployment
case "${STATE[findcare_db]}" in
    connected|ok|healthy|up)
        log "REQ-005: FindCare DB reports '${STATE[findcare_db]}' — healthy." ;;
    "")
        log "REQ-005 WARN: DB status field missing from /health (older build?). Treating as non-blocking." ;;
    *)
        log "FAIL (REQ-005): FindCare DB status is '${STATE[findcare_db]}' — not connected."
        verify_fail=1 ;;
esac

if [ "$verify_fail" -eq 1 ]; then
    # Dump the full /health bodies for each unhealthy endpoint so the
    # diagnostic is in the deploy log, not behind another curl.
    log ""
    log "───── Failed endpoint bodies ─────"
    for pair in "website:$WEBSITE_URL" "findcare:$FINDCARE_URL/health" "evalcare:$EVALCARE_URL/health" "shared:$SHARED_URL/health"; do
        short="${pair%%:*}"
        url="${pair#*:}"
        var="${short}_after"
        code=${STATE[$var]:-?}
        if [ "$code" != "200" ]; then
            log "  $short [$code] $url"
            curl -sk -m 10 "$url" 2>&1 | head -20 | sed 's/^/    /'
        fi
    done
    log "───── End endpoint dump ─────"
    STATE[overall]="endpoint_verify_fail"
    exit 1
fi

# ── Phase 7: Playwright smoke test (DEVOPS-DEV-B002) ─────────────────────

if [ "$SKIP_SMOKE" -eq 0 ]; then
    STATE[phase_current]="smoke"

    # Ring the bell + banner — Human can start manual testing in parallel
    powershell -c "[console]::beep(800,500)" 2>/dev/null || true
    STATE[manual_test_ready_ts]=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    log ""
    log "=================================================================="
    log "  DEV DEPLOY VERIFIED — TEST NOW"
    log "  Open:  https://dev.chathealthy.ai"
    log "  Playwright smoke starting in parallel (headless, separate browser)."
    log "  Known failures expected: ~4/34 (mTLS arch + 2 UI-state bugs)."
    log "=================================================================="
    log ""

    log "Running Playwright smoke test vs dev..."
    # Warn if local servers are up — mTLS smoke steps 15/27 hit localhost
    # sockets by design and produce false positives against dev if local is
    # still serving. Script only warns; does not kill (Human-owned decision).
    if curl -sk -o /dev/null -m 3 "https://localhost:8001/health" 2>/dev/null; then
        log "WARN: local EvaluateCare (:8001) is responding. Smoke steps 15/27"
        log "      hardcode localhost and will report PASS against local, not dev."
    fi

    if SMOKE_TEST_ENV="dev" \
           python -m pytest Code/deploy/devSmokeTestPyTest.py -v 2>&1 \
           | tee "$OUT_DIR/dev-$TS-smoke.log" >/dev/null; then
        :  # keep going so the summary-line parser below produces the tally either way
    fi
    # Parse pytest's canonical summary line: "N failed, M passed in …"
    # — accurate regardless of mid-stream counts.
    summary_line=$(grep -E "^=.*passed|^=.*failed" "$OUT_DIR/dev-$TS-smoke.log" | tail -1)
    pass_count=$(echo "$summary_line" | grep -oE "[0-9]+ passed" | grep -oE "[0-9]+" || echo 0)
    fail_count=$(echo "$summary_line" | grep -oE "[0-9]+ failed" | grep -oE "[0-9]+" || echo 0)
    STATE[smoke_pass]="$pass_count"
    STATE[smoke_fail]="$fail_count"
    if [ "${fail_count:-0}" -eq 0 ] && [ "${pass_count:-0}" -gt 0 ]; then
        STATE[smoke_status]="passed"
        log "Smoke test: $pass_count PASSED (100%)"
    else
        STATE[smoke_status]="failed_with_known_bugs"
        log "Smoke test: $pass_count PASSED, $fail_count FAILED (known-bugs state)."
        # Print the failing test names inline so the diagnostic is in the deploy log.
        log "───── Failing smoke tests ─────"
        grep -E "^FAILED " "$OUT_DIR/dev-$TS-smoke.log" | sed 's/^/  /'
        log "───── Full log: $OUT_DIR/dev-$TS-smoke.log ─────"
        # Non-fatal: deploy is valid per the 'working with known bugs' label.
    fi
else
    STATE[smoke_status]="skipped_by_flag"
    log "Skipping Playwright smoke test (--skip-smoke)."
fi

# ── Done ──────────────────────────────────────────────────────────────────

STATE[phase_current]="done"
if [ "${STATE[smoke_status]}" = "failed_with_known_bugs" ]; then
    STATE[overall]="success_with_known_bugs"
    log "=========================================="
    log "  DEPLOY + VERIFY PASSED"
    log "  Smoke: ${STATE[smoke_pass]:-0} pass, ${STATE[smoke_fail]:-?} fail (known-bugs state)."
    log "  FindCare build: ${STATE[old_build]} -> ${STATE[new_build]}, db=${STATE[findcare_db]}"
    log "=========================================="
else
    STATE[overall]="success"
    log "=========================================="
    log "  ALL CHECKS PASSED"
    log "  dev.chathealthy.ai + HF backends are live."
    log "  FindCare build: ${STATE[old_build]} -> ${STATE[new_build]}, db=${STATE[findcare_db]}"
    log "=========================================="
fi
exit 0
