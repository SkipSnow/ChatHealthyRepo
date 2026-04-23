#!/usr/bin/env bash
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# deploy_dev.sh — Deploy FindCare to dev (HuggingFace Space).
# One script. Does everything the CI does. No manual steps.
#
# Usage:  bash Code/deploy/deploy_dev.sh
#
# What it does:
#   1. Validates prerequisites (git clean, on dev branch, env vars)
#   2. Runs pre-deploy rule check
#   3. Bumps build counter in MongoDB
#   4. Commits and pushes to dev (triggers GitHub Actions → HF deploy)
#   5. Waits for HF container to come up with new build
#   6. Runs verification tests against dev.chathealthy.ai

set -e

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Activate project venv — script requires venv packages (pymongo for bump_build, etc.)
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

BACKEND_DIR="$REPO_ROOT/Code/ConversationalUX/FindCareChat/backend"
HF_HEALTH="https://skipsnow-dev-chathealthyspace.hf.space/health"

echo "=========================================="
echo "  FindCare Deploy to DEV"
echo "  Target: dev.chathealthy.ai"
echo "=========================================="
echo ""

# ── Step 1: Validate ─────────────────────────────────────────────
echo "[1/6] Validating prerequisites..."

BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)
if [ "$BRANCH" != "dev" ]; then
    echo "  ERROR: Must be on dev branch. Currently on: $BRANCH"
    exit 1
fi

# Check for uncommitted changes
if [ -n "$(git -C "$REPO_ROOT" status --porcelain)" ]; then
    echo "  WARNING: Uncommitted changes detected."
    echo "  Commit your changes first, or this deploy will not include them."
    git -C "$REPO_ROOT" status --short
    echo ""
    read -p "  Continue anyway? (y/N) " -n 1 -r
    echo ""
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

echo "  Branch: $BRANCH"
echo "  Done."

# ── Step 2: Pre-deploy rule check ────────────────────────────────
echo "[2/6] Running pre-deploy rule check..."
cd "$REPO_ROOT"
python Code/Shared/ops/tools/pre_deploy_rule_check.py findcare
echo "  Done."

# ── Step 3: Push to dev ──────────────────────────────────────────
echo "[3/6] Pushing to dev..."

# Get current build before push
OLD_BUILD=$(curl -s "$HF_HEALTH" 2>/dev/null | python -c "import sys,json; print(json.load(sys.stdin).get('build','?'))" 2>/dev/null || echo "?")
echo "  Current build on dev: $OLD_BUILD"

git -C "$REPO_ROOT" push origin dev
echo "  Pushed. GitHub Actions will build and deploy to HuggingFace."

# ── Step 4: Wait for GitHub Actions ──────────────────────────────
echo "[4/6] Waiting for GitHub Actions to complete..."

for i in $(seq 1 60); do
    STATUS=$(cd "$REPO_ROOT" && gh run list --limit 1 --branch dev --json status --jq '.[0].status' 2>/dev/null || echo "unknown")
    if [ "$STATUS" = "completed" ]; then
        CONCLUSION=$(cd "$REPO_ROOT" && gh run list --limit 1 --branch dev --json conclusion --jq '.[0].conclusion' 2>/dev/null || echo "unknown")
        if [ "$CONCLUSION" = "success" ]; then
            echo "  GitHub Actions: SUCCESS"
            break
        else
            echo "  ERROR: GitHub Actions failed with: $CONCLUSION"
            exit 1
        fi
    fi
    printf "  Waiting... (%ds)\r" $((i * 5))
    sleep 5
done

# ── Step 5: Wait for HF container ────────────────────────────────
echo "[5/6] Waiting for HuggingFace container to rebuild..."

for i in $(seq 1 60); do
    NEW_BUILD=$(curl -s "$HF_HEALTH" 2>/dev/null | python -c "import sys,json; print(json.load(sys.stdin).get('build','?'))" 2>/dev/null || echo "?")
    if [ "$NEW_BUILD" != "$OLD_BUILD" ] && [ "$NEW_BUILD" != "?" ]; then
        echo "  New build: $NEW_BUILD (was $OLD_BUILD)"
        break
    fi
    printf "  Waiting for build > %s... (%ds)\r" "$OLD_BUILD" $((i * 5))
    sleep 5
done

# ── Step 6: Verify ───────────────────────────────────────────────
echo "[6/6] Running verification against dev.chathealthy.ai..."

PASS=0
FAIL=0

# Test 1: Health endpoint
HEALTH=$(curl -s "$HF_HEALTH" 2>/dev/null)
HEALTH_STATUS=$(echo "$HEALTH" | python -c "import sys,json; print(json.load(sys.stdin).get('status','?'))" 2>/dev/null || echo "?")
if [ "$HEALTH_STATUS" = "ok" ]; then
    echo "  PASS: Health endpoint OK"
    PASS=$((PASS+1))
else
    echo "  FAIL: Health returned $HEALTH_STATUS"
    FAIL=$((FAIL+1))
fi

# Test 2: DB connected
DB_STATUS=$(echo "$HEALTH" | python -c "import sys,json; print(json.load(sys.stdin).get('db','?'))" 2>/dev/null || echo "?")
if [ "$DB_STATUS" = "connected" ]; then
    echo "  PASS: MongoDB connected"
    PASS=$((PASS+1))
else
    echo "  FAIL: DB status: $DB_STATUS"
    FAIL=$((FAIL+1))
fi

# Test 3: Chat endpoint responds
CHAT_RESP=$(curl -s -X POST "$HF_HEALTH/../chat" -H "Content-Type: application/json" -d '{"message":"hello","history":[]}' 2>/dev/null)
CHAT_OK=$(echo "$CHAT_RESP" | python -c "import sys,json; d=json.load(sys.stdin); print('ok' if d.get('response') and not d.get('error') else 'fail')" 2>/dev/null || echo "fail")
if [ "$CHAT_OK" = "ok" ]; then
    echo "  PASS: /chat endpoint responds"
    PASS=$((PASS+1))
else
    echo "  FAIL: /chat endpoint error"
    FAIL=$((FAIL+1))
fi

# Test 4: Welcome endpoint
WELCOME=$(curl -s "https://skipsnow-dev-chathealthyspace.hf.space/welcome" 2>/dev/null)
WELCOME_OK=$(echo "$WELCOME" | python -c "import sys,json; print('ok' if 'message' in json.load(sys.stdin) else 'fail')" 2>/dev/null || echo "fail")
if [ "$WELCOME_OK" = "ok" ]; then
    echo "  PASS: /welcome endpoint responds"
    PASS=$((PASS+1))
else
    echo "  FAIL: /welcome endpoint error"
    FAIL=$((FAIL+1))
fi

echo ""
echo "=========================================="
if [ "$FAIL" -eq 0 ]; then
    echo "  ALL $PASS TESTS PASSED"
    echo "  dev.chathealthy.ai is live."
    echo "  Build: $NEW_BUILD"
else
    echo "  $FAIL TESTS FAILED ($PASS passed)"
fi
echo "=========================================="
