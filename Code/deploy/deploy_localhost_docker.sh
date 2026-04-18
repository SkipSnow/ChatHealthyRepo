#!/usr/bin/env bash
# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# deploy_localhost_docker.sh — Local deploy that runs each backend in
# Docker, built from the same Dockerfile + requirements.txt HuggingFace
# uses. No ambient venv cheat. If a runtime import is missing from
# requirements.txt, the container fails to start the same way HF would.
#
# Produces three containers:
#   ch-findcare   — port 7860 (FastAPI + React static)
#   ch-evalcare   — port 8001 (FastAPI)
#   ch-sharedsvc  — port 8002 (FastAPI)
#
# Each container:
#   - Mounts Code/Shared/ops/certs/ read-only at /certs
#   - Sets CERTS_DIR=/certs
#   - Cross-service URLs point at host.docker.internal:<peer-port>
#   - Env vars loaded from Code/.env
#
# Website wrapper (port 443) and HTTP redirect (port 80) are NOT started
# by this script — those are separate concerns not yet containerized.
#
# Usage:  bash Code/deploy/deploy_localhost_docker.sh [--no-build] [--teardown]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CERTS_DIR_HOST="$REPO_ROOT/Code/Shared/ops/certs"
STAGE_ROOT="$REPO_ROOT/test_output/docker_stage"
ENV_FILE="$REPO_ROOT/Code/.env"

NO_BUILD=0
TEARDOWN_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --no-build)    NO_BUILD=1 ;;
        --teardown)    TEARDOWN_ONLY=1 ;;
    esac
done

CONTAINERS=(ch-findcare ch-evalcare ch-sharedsvc)

echo "=========================================="
echo "  ChatHealthy local Docker deploy"
echo "=========================================="

# ── Teardown ───────────────────────────────────────────────
echo "[1/6] Teardown existing containers + host python..."
for c in "${CONTAINERS[@]}"; do
    if docker ps -a --format '{{.Names}}' | grep -qx "$c"; then
        docker rm -f "$c" >/dev/null
        echo "  removed $c"
    fi
done
# Kill any prior host python processes (website wrapper from a previous run).
# Docker daemon + containers are not python.exe on the host, so this is safe
# for the containers we just re-created.
taskkill //F //IM python.exe >/dev/null 2>&1 || true

if [ "$TEARDOWN_ONLY" = "1" ]; then
    echo "  teardown complete. exit."
    exit 0
fi

# ── Prereqs ────────────────────────────────────────────────
echo "[2/6] Validating prerequisites..."
[ -f "$ENV_FILE" ] || { echo "  ERROR: $ENV_FILE not found"; exit 1; }
[ -d "$CERTS_DIR_HOST" ] || { echo "  ERROR: $CERTS_DIR_HOST not found"; exit 1; }
for cert in findcare.crt findcare.key evalcare.crt evalcare.key shared.crt shared.key ca.crt; do
    [ -f "$CERTS_DIR_HOST/$cert" ] || { echo "  ERROR: cert missing: $cert"; exit 1; }
done
command -v docker >/dev/null || { echo "  ERROR: docker not installed"; exit 1; }
echo "  prereqs OK."

# ── Assemble staging dirs (mirror HF workflows) ───────────
echo "[3/6] Assembling staging dirs..."
rm -rf "$STAGE_ROOT"
mkdir -p "$STAGE_ROOT"

# Helper: copy a tree then scrub excludes (cp lacks --exclude; rsync not on Windows git-bash)
scrub_excludes() {
    local dir="$1"
    find "$dir" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
    find "$dir" -type f -name '*.pyc'       -exec rm -f  {} + 2>/dev/null || true
    find "$dir" -type f -name '.env'        -exec rm -f  {} + 2>/dev/null || true
}

# FindCare — mirrors .github/workflows/deploy-findcare-backend.yml
FC_STAGE="$STAGE_ROOT/findcare"
mkdir -p "$FC_STAGE"
cp -r "$REPO_ROOT/Code/ConversationalUX/FindCareChat/backend/." "$FC_STAGE/"
scrub_excludes "$FC_STAGE"
# Frontend dist (if present) → static/
if [ -d "$REPO_ROOT/Code/ConversationalUX/FindCareChat/frontend/dist" ]; then
    rm -rf "$FC_STAGE/static"
    cp -r "$REPO_ROOT/Code/ConversationalUX/FindCareChat/frontend/dist" "$FC_STAGE/static"
fi
# Shared *.py at root
for f in "$REPO_ROOT/Code/Shared/"*.py; do cp "$f" "$FC_STAGE/"; done
# Brain artifacts
mkdir -p "$FC_STAGE/brain/machine_artifacts/content"
cp "$REPO_ROOT/brain/machine_artifacts/content/emergency_keywords.json" \
   "$FC_STAGE/brain/machine_artifacts/content/" 2>/dev/null || true
cp "$REPO_ROOT/brain/machine_artifacts/content/version.json" \
   "$FC_STAGE/brain/machine_artifacts/content/" 2>/dev/null || true
# me/ context
if [ -d "$REPO_ROOT/Code/ConversationalUX/ChatHealthyWhoAmIChat/me" ]; then
    mkdir -p "$FC_STAGE/me"
    cp -r "$REPO_ROOT/Code/ConversationalUX/ChatHealthyWhoAmIChat/me/." "$FC_STAGE/me/"
fi
# ops/ (uat_report)
mkdir -p "$FC_STAGE/ops"
cp "$REPO_ROOT/Code/Shared/ops/uat_report.py" "$FC_STAGE/ops/" 2>/dev/null || true
echo "  findcare staged: $(du -sh "$FC_STAGE" | awk '{print $1}')"

# EvaluateCare — mirrors deploy-evaluatecare-backend.yml
EC_STAGE="$STAGE_ROOT/evalcare"
mkdir -p "$EC_STAGE"
cp "$REPO_ROOT/Code/evaluate_care/app.py"          "$EC_STAGE/"
cp "$REPO_ROOT/Code/evaluate_care/Dockerfile"      "$EC_STAGE/"
cp "$REPO_ROOT/Code/evaluate_care/requirements.txt" "$EC_STAGE/"
mkdir -p "$EC_STAGE/evaluate_care"
cp -r "$REPO_ROOT/Code/evaluate_care/." "$EC_STAGE/evaluate_care/"
# Remove files we already placed at root
rm -f "$EC_STAGE/evaluate_care/app.py" \
      "$EC_STAGE/evaluate_care/Dockerfile" \
      "$EC_STAGE/evaluate_care/requirements.txt"
rm -rf "$EC_STAGE/evaluate_care/tests"
scrub_excludes "$EC_STAGE"
for f in "$REPO_ROOT/Code/Shared/"*.py; do cp "$f" "$EC_STAGE/"; done
echo "  evalcare staged: $(du -sh "$EC_STAGE" | awk '{print $1}')"

# SharedServices — mirrors deploy-shared-services.yml
SS_STAGE="$STAGE_ROOT/sharedsvc"
mkdir -p "$SS_STAGE"
cp -r "$REPO_ROOT/Code/shared_services/." "$SS_STAGE/"
scrub_excludes "$SS_STAGE"
for f in "$REPO_ROOT/Code/Shared/"*.py; do cp "$f" "$SS_STAGE/"; done
echo "  sharedsvc staged: $(du -sh "$SS_STAGE" | awk '{print $1}')"

# ── Build ─────────────────────────────────────────────────
if [ "$NO_BUILD" != "1" ]; then
    echo "[4/6] Building images (this is where missing deps surface)..."
    docker build -t ch-findcare  "$FC_STAGE"   2>&1 | tail -8
    docker build -t ch-evalcare  "$EC_STAGE"   2>&1 | tail -8
    docker build -t ch-sharedsvc "$SS_STAGE"   2>&1 | tail -8
else
    echo "[4/6] Skipping build (--no-build)"
fi

# ── Sanitize .env ──────────────────────────────────────────
# Keep only lines matching KEY=VALUE (Code/.env currently has a malformed
# first line 'ok l#...' that docker --env-file rejects).
CLEAN_ENV="$STAGE_ROOT/.env.clean"
grep -E '^[A-Za-z_][A-Za-z0-9_]*=' "$ENV_FILE" > "$CLEAN_ENV"

# ── Run ───────────────────────────────────────────────────
echo "[5/6] Starting containers..."

# Git-bash path-mangling: /something gets rewritten to C:/Program Files/Git/something
# on the docker CLI. We can't blanket-disable MSYS_NO_PATHCONV because $CLEAN_ENV is
# a legitimate git-bash path that needs Windows-translation for --env-file.
# Trick: for literal POSIX paths inside the container, use a double-slash prefix —
# git-bash collapses '//foo' to '/foo' without trying to convert to a Windows path.
CERTS_MOUNT="//certs"

# On Windows, Docker wants a Windows-style host path for bind mounts.
# git-bash native /c/foo paths silently mount nothing.
if command -v cygpath >/dev/null 2>&1; then
    CERTS_DIR_HOST_DOCKER="$(cygpath -w "$CERTS_DIR_HOST")"
else
    CERTS_DIR_HOST_DOCKER="$CERTS_DIR_HOST"
fi
echo "  certs mount: $CERTS_DIR_HOST_DOCKER -> $CERTS_MOUNT"

# Cross-service URLs — containers reach the host via host.docker.internal.
# Docker Desktop (Windows + macOS) wires this alias automatically.
# On bare Linux docker, --add-host=host.docker.internal:host-gateway is needed;
# Docker Desktop accepts the flag harmlessly, so pass it unconditionally.
HOST_ALIAS="host.docker.internal"
EXTRA_HOST_FLAG="--add-host=host.docker.internal:host-gateway"

# SSL env: each container terminates TLS inside using its own cert (findcare.crt,
# evalcare.crt, shared.crt) and corresponding key. Satisfies DEVOPS-LOCAL-B003.
docker run -d --name ch-evalcare \
    -p 8001:7860 \
    -v "$CERTS_DIR_HOST_DOCKER:$CERTS_MOUNT:ro" \
    --env-file "$CLEAN_ENV" \
    -e "CERTS_DIR=$CERTS_MOUNT" \
    -e PORT=7860 \
    -e ENV_PREFIX=dev \
    -e "SSL_CERTFILE=$CERTS_MOUNT/evalcare.crt" \
    -e "SSL_KEYFILE=$CERTS_MOUNT/evalcare.key" \
    $EXTRA_HOST_FLAG \
    ch-evalcare python app.py >/dev/null
echo "  ch-evalcare  on :8001 (HTTPS)"

docker run -d --name ch-sharedsvc \
    -p 8002:7860 \
    -v "$CERTS_DIR_HOST_DOCKER:$CERTS_MOUNT:ro" \
    --env-file "$CLEAN_ENV" \
    -e "CERTS_DIR=$CERTS_MOUNT" \
    -e PORT=7860 \
    -e ENV_PREFIX=dev \
    -e "SSL_CERTFILE=$CERTS_MOUNT/shared.crt" \
    -e "SSL_KEYFILE=$CERTS_MOUNT/shared.key" \
    $EXTRA_HOST_FLAG \
    ch-sharedsvc python app.py >/dev/null
echo "  ch-sharedsvc on :8002 (HTTPS)"

docker run -d --name ch-findcare \
    -p 7860:7860 \
    -v "$CERTS_DIR_HOST_DOCKER:$CERTS_MOUNT:ro" \
    --env-file "$CLEAN_ENV" \
    -e "CERTS_DIR=$CERTS_MOUNT" \
    -e PORT=7860 \
    -e ENV_PREFIX=dev \
    -e "SSL_CERTFILE=$CERTS_MOUNT/findcare.crt" \
    -e "SSL_KEYFILE=$CERTS_MOUNT/findcare.key" \
    -e EVALCARE_URL="https://$HOST_ALIAS:8001" \
    -e SHARED_SERVICES_URL="https://$HOST_ALIAS:8002" \
    $EXTRA_HOST_FLAG \
    ch-findcare >/dev/null
echo "  ch-findcare  on :7860 (HTTPS)"

# ── Verify ─────────────────────────────────────────────────
echo "[6/6] Waiting for health..."
FAIL=0
for i in $(seq 1 60); do
    ALL_UP=1
    for p in 7860 8001 8002; do
        curl -sk -o /dev/null -w '' "https://localhost:$p/health" || ALL_UP=0
    done
    [ "$ALL_UP" = "1" ] && break
    sleep 1
done

for p_name in "7860:findcare" "8001:evalcare" "8002:shared_services"; do
    port="${p_name%:*}"; name="${p_name#*:}"
    body=$(curl -sk "https://localhost:$port/health" || true)
    if echo "$body" | grep -q '"status":"ok"'; then
        echo "  PASS :$port $name — $body"
    else
        echo "  FAIL :$port $name — $body"
        echo "  ---- last 30 log lines ----"
        docker logs --tail 30 "ch-${name%_*}" 2>&1 || true
        echo "  ---------------------------"
        FAIL=1
    fi
done

if [ "$FAIL" != "0" ]; then
    echo ""
    echo "=========================================="
    echo "  FAILURES — see logs above."
    echo "=========================================="
    exit 1
fi

# ── Start website wrapper (port 443) on host ──────────────
# B014 scopes Docker to the 3 backends; website wrapper is separate.
# _start_website.py also serves the :80 HTTP→HTTPS redirect.
echo "[7/7] Starting website wrapper on :443 (host python)..."
WEBSITE_DIR="$REPO_ROOT/Website"
if command -v cygpath >/dev/null 2>&1; then
    CERTS_WIN="$(cygpath -w "$CERTS_DIR_HOST")"
    WEBSITE_WIN="$(cygpath -w "$WEBSITE_DIR")"
else
    CERTS_WIN="$CERTS_DIR_HOST"
    WEBSITE_WIN="$WEBSITE_DIR"
fi

python "$REPO_ROOT/Code/deploy/_start_website.py" "$CERTS_WIN" "$WEBSITE_WIN" \
    > "$STAGE_ROOT/website.log" 2>&1 &
WEBSITE_PID=$!
echo "  website PID=$WEBSITE_PID (log: $STAGE_ROOT/website.log)"

# Wait for :443
for i in $(seq 1 30); do
    if curl -sk -o /dev/null -w '' "https://localhost/" 2>/dev/null; then
        echo "  website up."
        break
    fi
    sleep 1
done

echo ""
echo "=========================================="
echo "  Stack up."
echo "  FindCare   https://localhost:7860/health"
echo "  EvalCare   https://localhost:8001/health"
echo "  SharedSvc  https://localhost:8002/health"
echo "  Website    https://localhost/"
echo "  HTTP redirect http://localhost -> https://localhost"
echo "=========================================="
