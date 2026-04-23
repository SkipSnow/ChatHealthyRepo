#!/usr/bin/env bash
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# deploy_localhost.sh — Local UAT deploy for FindCare.
#
# Architecture:
#   Port 80   → HTTP redirect to HTTPS
#   Port 443  → Website wrapper (banner, header, iframe → FindCare)
#   Port 7860 → FindCare backend + React frontend (iframe source)
#   Port 8001 → EvaluateCare (HTTPS)
#   Port 8002 → CHShared (HTTPS)
#
# Each server runs as a SEPARATE PROCESS for isolation.
#
# Usage:  bash Code/deploy/deploy_localhost.sh

set -e

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

# Activate project venv — script requires venv packages (uvicorn, pymongo, httpx, etc.)
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

FRONTEND_DIR="$REPO_ROOT/Code/ConversationalUX/FindCareChat/frontend"
BACKEND_DIR="$REPO_ROOT/Code/ConversationalUX/FindCareChat/backend"
WEBSITE_DIR="$REPO_ROOT/Website"
CERTS_DIR="$REPO_ROOT/Code/Shared/ops/certs"
DEPLOY_DIR="$REPO_ROOT/Code/deploy"

OUTPUT_DIR="$REPO_ROOT/test_output/deploy"
mkdir -p "$OUTPUT_DIR"
OUTPUT_FILE="$OUTPUT_DIR/deploy_localhost_$(date +%Y%m%d_%H%M%S).log"

# Tee all output to file and stdout
exec > >(tee -a "$OUTPUT_FILE") 2>&1

echo "=========================================="
echo "  FindCare Local UAT Deploy"
echo "=========================================="
echo "  Output: $OUTPUT_FILE"
echo "  Port 80   → HTTP redirect"
echo "  Port 443  → Website (banner + iframe)"
echo "  Port 7860 → FindCare backend + React"
echo "  Port 8001 → EvaluateCare (HTTPS)"
echo "  Port 8002 → CHShared (HTTPS)"
echo "=========================================="
echo ""

# ── Step 1: Kill everything ──────────────────────────────────
echo "[1/7] Teardown — checking existing servers and processes..."

# Check each port before teardown
for PORT in 80 443 7860 8001 8002; do
    if curl -sk -o /dev/null -w "%{http_code}" "https://localhost:${PORT}/health" 2>/dev/null | grep -q "200"; then
        echo "  Port $PORT: SERVER RUNNING — will be killed"
    elif curl -s -o /dev/null -w "%{http_code}" "http://localhost:${PORT}/" 2>/dev/null | grep -qE "200|301"; then
        echo "  Port $PORT: SERVER RUNNING (HTTP) — will be killed"
    else
        echo "  Port $PORT: empty"
    fi
done

# Check for existing python/node processes
PY_COUNT=$(tasklist //FI "IMAGENAME eq python.exe" 2>/dev/null | grep -c "python.exe" || echo "0")
NODE_COUNT=$(tasklist //FI "IMAGENAME eq node.exe" 2>/dev/null | grep -c "node.exe" || echo "0")
echo "  Python processes: $PY_COUNT"
echo "  Node processes: $NODE_COUNT"

# Kill processes
if [ "$PY_COUNT" -gt 0 ] 2>/dev/null; then
    echo "  Killing $PY_COUNT Python processes..."
    taskkill //F //FI "IMAGENAME eq python.exe" 2>/dev/null || true
else
    echo "  No Python processes to kill."
fi

if [ "$NODE_COUNT" -gt 0 ] 2>/dev/null; then
    echo "  Killing $NODE_COUNT Node processes..."
    taskkill //F //FI "IMAGENAME eq node.exe" 2>/dev/null || true
else
    echo "  No Node processes to kill."
fi

sleep 2

# Verify ports are clear after teardown
for PORT in 80 443 7860 8001 8002; do
    if curl -sk -o /dev/null -w "%{http_code}" "https://localhost:${PORT}/" 2>/dev/null | grep -q "200"; then
        echo "  WARNING: Port $PORT still responding after teardown"
    fi
done

echo "  Teardown complete."

# ── Step 2: Wipe old artifacts ───────────────────────────────
echo "[2/7] Wiping old build artifacts..."
rm -rf "$FRONTEND_DIR/dist"
rm -rf "$FRONTEND_DIR/node_modules/.vite"
echo "  Done."

# ── Step 3: Validate prerequisites ───────────────────────────
echo "[3/7] Validating prerequisites..."

for cert in localhost.crt localhost.key findcare.crt findcare.key evalcare.crt evalcare.key shared.crt shared.key ca.crt; do
    if [ ! -f "$CERTS_DIR/$cert" ]; then
        echo "  ERROR: $cert not found in $CERTS_DIR"
        exit 1
    fi
done

if ! command -v node &>/dev/null; then echo "  ERROR: Node.js not installed."; exit 1; fi
if ! command -v python &>/dev/null; then echo "  ERROR: Python not found."; exit 1; fi

# Fix Windows symlinks
UX_LINK="$FRONTEND_DIR/src/ux"
UX_TARGET="$REPO_ROOT/Code/Shared/ux"
if [ ! -d "$UX_LINK" ]; then
    rm -f "$UX_LINK"
    cmd //c "mklink /D \"$(cygpath -w "$UX_LINK")\" \"$(cygpath -w "$UX_TARGET")\"" 2>/dev/null || cp -r "$UX_TARGET" "$UX_LINK"
fi
[ -d "$UX_LINK" ] || { echo "  ERROR: ux symlink broken"; exit 1; }

echo "  Done."

# ── Step 4: Build React frontend ─────────────────────────────
echo "[4/7] Building React frontend..."
cd "$FRONTEND_DIR"
npm ci --silent 2>&1 | tail -1
VITE_API_URL="" npx vite build 2>&1 | tail -3

[ -f "$FRONTEND_DIR/dist/index.html" ] || { echo "  ERROR: Frontend build failed."; exit 1; }

# Copy to backend static
rm -rf "$BACKEND_DIR/static/assets" "$BACKEND_DIR/static/index.html"
cp -r "$FRONTEND_DIR/dist/"* "$BACKEND_DIR/static/"
echo "  Done."

# ── Step 5: Start all servers (separate processes) ────────────
echo "[5/7] Starting servers..."

CERTS_WIN=$(cygpath -w "$CERTS_DIR" 2>/dev/null || echo "$CERTS_DIR")
WEBSITE_WIN=$(cygpath -w "$WEBSITE_DIR" 2>/dev/null || echo "$WEBSITE_DIR")

# Each server is a separate process — no shared state
python "$DEPLOY_DIR/_start_website.py" "$CERTS_WIN" "$WEBSITE_WIN" &
PID_WEBSITE=$!
echo "  Website     (443)  PID=$PID_WEBSITE"

python "$DEPLOY_DIR/_start_findcare.py" "$CERTS_WIN" &
PID_FINDCARE=$!
echo "  FindCare    (7860) PID=$PID_FINDCARE"

python "$DEPLOY_DIR/_start_evalcare.py" "$CERTS_WIN" &
PID_EVALCARE=$!
echo "  EvaluateCare(8001) PID=$PID_EVALCARE"

python "$DEPLOY_DIR/_start_shared.py" "$CERTS_WIN" &
PID_SHARED=$!
echo "  CHShared    (8002) PID=$PID_SHARED"

# Wait for all servers — up to 120s (FindCare needs MongoDB)
echo "  Waiting for all servers..."
for i in $(seq 1 60); do
    ALL_UP=true
    curl -sk https://localhost/ -o /dev/null 2>/dev/null || ALL_UP=false
    curl -sk https://localhost:7860/health -o /dev/null 2>/dev/null || ALL_UP=false
    curl -sk https://localhost:8001/health -o /dev/null 2>/dev/null || ALL_UP=false
    curl -sk https://localhost:8002/health -o /dev/null 2>/dev/null || ALL_UP=false

    if [ "$ALL_UP" = true ]; then
        echo "  All servers up."
        break
    fi
    sleep 2
done

# ── Step 6: Verify ───────────────────────────────────────────
echo ""
echo "[6/7] Running verification..."
echo ""

PASS=0
FAIL=0

# Test 1: HTTP redirect
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" http://localhost/ 2>/dev/null)
if [ "$HTTP_CODE" = "301" ]; then
    echo "  PASS [1/9]: http://localhost → 301 redirect"
    PASS=$((PASS+1))
else
    echo "  FAIL [1/9]: http://localhost returned $HTTP_CODE (expected 301)"
    FAIL=$((FAIL+1))
fi

# Test 2: Website serves on HTTPS
HTTPS_CODE=$(curl -sk -o /dev/null -w "%{http_code}" https://localhost/ 2>/dev/null)
if [ "$HTTPS_CODE" = "200" ]; then
    echo "  PASS [2/9]: https://localhost → 200 OK"
    PASS=$((PASS+1))
else
    echo "  FAIL [2/9]: https://localhost returned $HTTPS_CODE"
    FAIL=$((FAIL+1))
fi

# Test 3: Website has banner
PAGE=$(curl -sk https://localhost/ 2>/dev/null)
if echo "$PAGE" | grep -q "envBanner"; then
    echo "  PASS [3/9]: Website has banner"
    PASS=$((PASS+1))
else
    echo "  FAIL [3/9]: Website missing banner"
    FAIL=$((FAIL+1))
fi

# Test 4: FindCare backend healthy — verify service identity
FC_HEALTH=$(curl -sk https://localhost:7860/health 2>/dev/null)
if echo "$FC_HEALTH" | grep -q '"status":"ok"' && echo "$FC_HEALTH" | grep -q '"env"'; then
    echo "  PASS [4/9]: FindCare backend healthy (port 7860)"
    PASS=$((PASS+1))
else
    echo "  FAIL [4/9]: FindCare backend unhealthy: $FC_HEALTH"
    FAIL=$((FAIL+1))
fi

# Test 5: EvaluateCare healthy — verify it's evaluate_care not shared_services
EC_HEALTH=$(curl -sk https://localhost:8001/health 2>/dev/null)
if echo "$EC_HEALTH" | grep -q '"service":"evaluate_care"'; then
    echo "  PASS [5/9]: EvaluateCare healthy (port 8001) — correct service"
    PASS=$((PASS+1))
else
    echo "  FAIL [5/9]: Port 8001 wrong service: $EC_HEALTH"
    FAIL=$((FAIL+1))
fi

# Test 6: CHShared healthy — verify it's shared_services
SH_HEALTH=$(curl -sk https://localhost:8002/health 2>/dev/null)
if echo "$SH_HEALTH" | grep -q '"service":"shared_services"'; then
    echo "  PASS [6/9]: CHShared healthy (port 8002) — correct service"
    PASS=$((PASS+1))
else
    echo "  FAIL [6/9]: Port 8002 wrong service: $SH_HEALTH"
    FAIL=$((FAIL+1))
fi

# Test 7: mTLS — FindCare can reach EvaluateCare with client cert
# Use Python httpx because Windows curl (schannel) can't read PEM certs
CERTS_ABS=$(cd "$CERTS_DIR" && pwd -W 2>/dev/null || pwd)
MTLS_EVAL=$(python -c "
import httpx, sys, os
d = sys.argv[1]
try:
    c = httpx.Client(cert=(os.path.join(d,'findcare.crt'), os.path.join(d,'findcare.key')), verify=os.path.join(d,'ca.crt'), timeout=10)
    r = c.get('https://localhost:8001/health')
    print(r.text)
    c.close()
except Exception as e:
    print(f'ERROR: {e}')
" "$CERTS_ABS" 2>&1 || true)
if echo "$MTLS_EVAL" | grep -q '"service":"evaluate_care"'; then
    echo "  PASS [7/9]: mTLS FindCare→EvaluateCare verified"
    PASS=$((PASS+1))
else
    echo "  FAIL [7/9]: mTLS FindCare→EvaluateCare failed: $MTLS_EVAL"
    FAIL=$((FAIL+1))
fi

# Test 8: mTLS — FindCare can reach CHShared with client cert
MTLS_SH=$(python -c "
import httpx, sys, os
d = sys.argv[1]
try:
    c = httpx.Client(cert=(os.path.join(d,'findcare.crt'), os.path.join(d,'findcare.key')), verify=os.path.join(d,'ca.crt'), timeout=10)
    r = c.get('https://localhost:8002/health')
    print(r.text)
    c.close()
except Exception as e:
    print(f'ERROR: {e}')
" "$CERTS_ABS" 2>&1 || true)
if echo "$MTLS_SH" | grep -q '"service":"shared_services"'; then
    echo "  PASS [8/9]: mTLS FindCare→CHShared verified"
    PASS=$((PASS+1))
else
    echo "  FAIL [8/9]: mTLS FindCare→CHShared failed: $MTLS_SH"
    FAIL=$((FAIL+1))
fi

# Test 9: Chat endpoint responds through LangGraph
CHAT_RESP=$(curl -sk -X POST "https://localhost:7860/chat" -H "Content-Type: application/json" -d '{"message":"hello","history":[]}' 2>&1 || true)
if echo "$CHAT_RESP" | python -c "import sys,json; d=json.load(sys.stdin); sys.exit(0 if d.get('response') and not d.get('error') else 1)" 2>/dev/null; then
    echo "  PASS [9/9]: /chat endpoint responds (LangGraph)"
    PASS=$((PASS+1))
else
    echo "  FAIL [9/9]: /chat endpoint error: $(echo "$CHAT_RESP" | head -c 200)"
    FAIL=$((FAIL+1))
fi

echo ""
echo "=========================================="
if [ "$FAIL" -eq 0 ]; then
    echo "  ALL $PASS/9 TESTS PASSED"
    echo "  http://localhost → https://localhost"
    echo "  Ready for UAT."
else
    echo "  $FAIL TESTS FAILED ($PASS passed)"
    echo "  Fix before testing."
fi
echo "=========================================="

# ── Step 7: Playwright smoke test (B009) ─────────────────────
if [ "$FAIL" -eq 0 ]; then
    echo ""
    echo "[7/8] Running Playwright smoke test (34 steps)..."
    cd "$REPO_ROOT"
    python -m pytest Code/deploy/localSmokeTestPyTest.py -v 2>&1 | tee -a "$OUTPUT_FILE"
    SMOKE_EXIT=${PIPESTATUS[0]}
    if [ "$SMOKE_EXIT" -eq 0 ]; then
        echo ""
        echo "=========================================="
        echo "  SMOKE TEST PASSED"
        echo "  Ready for human UAT at http://localhost"
        echo "=========================================="
    else
        echo ""
        echo "=========================================="
        echo "  SMOKE TEST FAILED"
        echo "  Fix before UAT."
        echo "=========================================="
    fi

    # ── Step 8: Keep running ─────────────────────────────────
    echo ""
    echo "[8/8] Servers running. Press Ctrl+C to stop."
    wait
fi
