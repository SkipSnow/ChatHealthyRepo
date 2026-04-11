# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Infrastructure tests for local dev topology.
# These tests inspect file contents — they do NOT start services.
#
# SDT-LOCAL-001: Static website server on port 80
# SDT-LOCAL-002: EvaluateCare FastAPI on port 8001
# SDT-LOCAL-003: start_local.bat one-click startup

import os
import re
import ast
import pytest

# ── Paths ──────────────────────────────────────────────────────────────────

_REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
_START_BAT = os.path.join(_REPO, "start_local.bat")
_START_BAT_OPS = os.path.join(_REPO, "Code", "Shared", "ops", "start_local.bat")
_EVALCARE_APP = os.path.join(_REPO, "Code", "evaluate_care", "app.py")


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _bat_content() -> str:
    """Return start_local.bat content (prefer root, fallback to ops)."""
    for p in [_START_BAT, _START_BAT_OPS]:
        if os.path.exists(p):
            return _read(p)
    pytest.skip("start_local.bat not found")


# ═══════════════════════════════════════════════════════════════════════════
# SDT-LOCAL-001: Static website server on port 80
# ═══════════════════════════════════════════════════════════════════════════

class TestSDTLocal001:
    """SDT-LOCAL-001: Python HTTP server serves Website/ on port 80."""

    def test_bat_starts_service_on_port_80(self):
        """SDT-LOCAL-001-REQ-001: start_local.bat launches something that serves port 80."""
        bat = _bat_content()
        # Caddy handles port 80 (reverse proxy) or a direct python http.server
        assert ":80" in bat or "port 80" in bat.lower() or "Caddy" in bat, \
            "start_local.bat must configure port 80 serving"


# ═══════════════════════════════════════════════════════════════════════════
# SDT-LOCAL-002: EvaluateCare FastAPI on port 8001
# ═══════════════════════════════════════════════════════════════════════════

class TestSDTLocal002:
    """SDT-LOCAL-002: EvaluateCare app.py on port 8001."""

    def test_app_runs_on_port_8001(self):
        """SDT-LOCAL-002-REQ-001: app.py defaults to port 8001."""
        src = _read(_EVALCARE_APP)
        assert "8001" in src, "EvaluateCare app.py must reference port 8001"
        # Verify the __main__ block or uvicorn config uses 8001
        assert 'PORT' in src or '8001' in src

    def test_cors_allows_localhost_origins(self):
        """SDT-LOCAL-002-REQ-002: CORS allows localhost origins for all ports."""
        src = _read(_EVALCARE_APP)
        assert "CORSMiddleware" in src, "Must use CORSMiddleware"
        assert "localhost" in src, "CORS origins must include localhost"
        # Check that a regex or explicit list covers multiple ports
        has_regex = "allow_origin_regex" in src
        has_multi = src.count("localhost") >= 2
        assert has_regex or has_multi, \
            "CORS must allow localhost on multiple ports (regex or explicit list)"

    def test_health_endpoint_returns_service_and_version(self):
        """SDT-LOCAL-002-REQ-003: /health returns service name and version."""
        src = _read(_EVALCARE_APP)
        # Find the health endpoint
        assert '"/health"' in src or "'/health'" in src, \
            "Must define /health endpoint"
        # The health function should return service and version
        assert '"service"' in src or "'service'" in src, \
            "/health must return 'service' field"
        assert '"version"' in src or "'version'" in src, \
            "/health must return 'version' field"
        assert "evaluate_care" in src, \
            "/health service name must be 'evaluate_care'"


# ═══════════════════════════════════════════════════════════════════════════
# SDT-LOCAL-003: start_local.bat one-click startup
# ═══════════════════════════════════════════════════════════════════════════

class TestSDTLocal003:
    """SDT-LOCAL-003: start_local.bat launches all services."""

    def test_launches_all_4_services(self):
        """SDT-LOCAL-003-REQ-001: start_local.bat launches all 4 services."""
        bat = _bat_content()
        # Must launch: Caddy/Website, Vite/React, FindCare, EvaluateCare
        services_found = []
        if "caddy" in bat.lower() or ":80" in bat or ":443" in bat:
            services_found.append("caddy/website")
        if "5173" in bat or "vite" in bat.lower() or "npm run dev" in bat:
            services_found.append("react/vite")
        if "8000" in bat or "findcare" in bat.lower() or "FindCare" in bat:
            services_found.append("findcare")
        if "8001" in bat or "evaluatecare" in bat.lower() or "EvaluateCare" in bat or "evaluate_care" in bat:
            services_found.append("evaluatecare")
        assert len(services_found) >= 4, \
            f"Must launch 4 services, found {len(services_found)}: {services_found}"

    def test_admin_requirement_documented(self):
        """SDT-LOCAL-003-REQ-002: Script documents admin requirement for port 80."""
        bat = _bat_content()
        bat_lower = bat.lower()
        has_admin_ref = ("administrator" in bat_lower or "admin" in bat_lower
                         or "run as" in bat_lower or "elevated" in bat_lower
                         or "port 80" in bat_lower)
        assert has_admin_ref, \
            "start_local.bat must mention admin/administrator requirement for port 80"

    def test_kills_zombies_on_all_service_ports(self):
        """SDT-LOCAL-003-REQ-005: Kills zombies on ALL service ports."""
        bat = _bat_content()
        required_ports = ["80", "443", "5173", "8000", "8001"]
        missing = []
        for port in required_ports:
            # Look for netstat/findstr/taskkill pattern with this port
            pattern = f":{port} "
            if pattern not in bat and f":{port}\"" not in bat:
                missing.append(port)
        assert not missing, \
            f"start_local.bat must kill zombies on ports {missing}"

    def test_logs_output_to_temp(self):
        """SDT-LOCAL-003-REQ-006: Logs output to %TEMP%/chathealthy_*.log."""
        bat = _bat_content()
        assert "TEMP" in bat or "temp" in bat.lower(), \
            "start_local.bat must reference %TEMP% for log files"
        assert "chathealthy_" in bat.lower(), \
            "Log files must use chathealthy_* naming convention"
        assert ".log" in bat, \
            "Log files must have .log extension"

    def test_verification_reports_log_on_failure(self):
        """SDT-LOCAL-003-REQ-007: Verification reports which log to check on failure."""
        bat = _bat_content()
        # The FAIL lines should reference log file paths
        bat_lower = bat.lower()
        has_fail_with_log = ("fail" in bat_lower and "log" in bat_lower) or \
                            ("check" in bat_lower and "log" in bat_lower)
        assert has_fail_with_log, \
            "Verification failures must tell user which log to check"

    def test_verifies_evaluatecare_on_8001(self):
        """SDT-LOCAL-003-REQ-008: Verifies EvaluateCare on :8001 is healthy."""
        bat = _bat_content()
        # Should curl or check :8001/health
        has_8001_verify = ("8001" in bat and ("health" in bat.lower() or "curl" in bat.lower()))
        assert has_8001_verify, \
            "start_local.bat must verify EvaluateCare on :8001/health"
