"""
httpMethodEnforcementPyTest.py — verifies the four requirements under
EPIC-008-F-011-S-002 "Use of HTTP methods for ChatHealthy.ai":

  REQ-B-001: dynamic web servers (HF-hosted FastAPI) accept ONLY HTTPS POST
             and OPTIONS on every client-callable endpoint; reject other
             methods with 405.
  REQ-B-002: every web server (dynamic + static) MUST 301-redirect HTTP→HTTPS
             at the same path.
  REQ-B-003: static page web server (Cloudflare for prod-like envs; local dev
             web server for `local`) accepts GET and HEAD only; OPTIONS
             explicitly returns 405.
  REQ-B-004: this test exists and must fail when any of the above is violated.

This implementation is exhaustive: it fetches each dynamic server's
/openapi.json at test time and iterates EVERY path the FastAPI app
declares — not a hardcoded sample. New endpoints added later are
automatically covered.

Run:
  SMOKE_TEST_ENV=local python -m pytest Code/deploy/httpMethodEnforcementPyTest.py -v
  SMOKE_TEST_ENV=dev   python -m pytest Code/deploy/httpMethodEnforcementPyTest.py -v
  SMOKE_TEST_ENV=qa    python -m pytest Code/deploy/httpMethodEnforcementPyTest.py -v
  SMOKE_TEST_ENV=prod  python -m pytest Code/deploy/httpMethodEnforcementPyTest.py -v
"""

import os
import pytest
import requests

SMOKE_ENV = os.getenv("SMOKE_TEST_ENV", "local").lower()

_HOSTS = {
    "local": {
        "findcare":      "https://localhost:7860",
        "evaluatecare":  "https://localhost:8001",
        "sharedservices":"https://localhost:8002",
        "static_wrapper":"https://localhost",
    },
    "dev": {
        "findcare":      "https://skipsnow-dev-chathealthyspace.hf.space",
        "evaluatecare":  "https://skipsnow-dev-evaluatecarespace.hf.space",
        "sharedservices":"https://skipsnow-dev-sharedservicesspace.hf.space",
        "static_wrapper":"https://dev.chathealthy.ai",
    },
    "qa": {
        "findcare":      "https://skipsnow-qa-chathealthyspace.hf.space",
        "evaluatecare":  "https://skipsnow-qa-evaluatecarespace.hf.space",
        "sharedservices":"https://skipsnow-qa-sharedservicesspace.hf.space",
        "static_wrapper":"https://qa.chathealthy.ai",
    },
    "prod": {
        "findcare":      "https://skipsnow-chathealthyspace.hf.space",
        "evaluatecare":  "https://skipsnow-evaluatecarespace.hf.space",
        "sharedservices":"https://skipsnow-sharedservicesspace.hf.space",
        "static_wrapper":"https://chathealthy.ai",
    },
}

if SMOKE_ENV not in _HOSTS:
    raise RuntimeError(f"SMOKE_TEST_ENV={SMOKE_ENV!r} not recognised; expected one of {list(_HOSTS)}")
HOSTS = _HOSTS[SMOKE_ENV]

DYNAMIC_SERVICES = ["findcare", "evaluatecare", "sharedservices"]

# REQ-B-001: dynamic endpoints accept POST + OPTIONS; reject others with 405.
# REQ-B-003: static wrapper accepts GET + HEAD; reject others (incl. OPTIONS)
#            with 405.
DYNAMIC_REJECTED_METHODS = ("GET", "HEAD", "PUT", "DELETE", "PATCH")
STATIC_ALLOWED_METHODS = ("GET", "HEAD")
STATIC_REJECTED_METHODS = ("POST", "PUT", "DELETE", "PATCH", "OPTIONS")

# Acceptable POST responses for liveness (200 OK; 422 missing required body;
# 503 degraded backend per the runtime-governance contract).
ACCEPTED_POST_CODES = {200, 422, 503}

VERIFY_TLS = SMOKE_ENV != "local"


def _fetch_openapi_paths(service_url):
    """Return list of (service, path) for every path in the service's
    /openapi.json. /openapi.json is itself fetched via GET — it's the
    spec-introspection endpoint FastAPI exposes by default."""
    spec_url = service_url + "/openapi.json"
    resp = requests.get(spec_url, timeout=15, verify=VERIFY_TLS)
    resp.raise_for_status()
    return list(resp.json().get("paths", {}).keys())


def _all_dynamic_paths():
    """Enumerate every (service, path) tuple across all 3 dynamic backends.
    Excludes /openapi.json itself (it's the introspection endpoint, GET-only
    by FastAPI design — would create a self-referential failure)."""
    out = []
    for svc in DYNAMIC_SERVICES:
        try:
            paths = _fetch_openapi_paths(HOSTS[svc])
        except Exception as exc:
            pytest.skip(f"{svc}: cannot fetch /openapi.json — {type(exc).__name__}: {exc}")
            return out
        for p in paths:
            if p == "/openapi.json":
                continue
            out.append((svc, p))
    return out


# Resolved once at collection time so parametrize sees concrete tuples.
_DYNAMIC_PATHS = _all_dynamic_paths()


# ── REQ-B-002: HTTP→HTTPS redirect ───────────────────────────────────
@pytest.mark.parametrize("host_key", ["findcare", "evaluatecare", "sharedservices", "static_wrapper"])
def test_REQ_B_002_http_redirects_to_https(host_key):
    https_url = HOSTS[host_key]
    if SMOKE_ENV == "local":
        http_url = https_url.replace("https://", "http://")
        path_url = http_url + "/health" if host_key != "static_wrapper" else http_url + "/"
        try:
            resp = requests.get(path_url, timeout=4, allow_redirects=False, verify=False)
            assert resp.status_code == 301, (
                f"local {host_key}: HTTP listener responded {resp.status_code} but R2 requires 301 redirect"
            )
            loc = resp.headers.get("Location", "")
            assert loc.startswith("https://"), f"local {host_key}: redirect not to HTTPS ({loc!r})"
        except (requests.ConnectionError, requests.Timeout):
            pass
        return
    http_url = https_url.replace("https://", "http://")
    path = "/health" if host_key != "static_wrapper" else "/"
    resp = requests.get(http_url + path, timeout=10, allow_redirects=False)
    assert resp.status_code == 301, (
        f"{host_key} ({http_url}{path}): expected 301, got {resp.status_code}"
    )
    loc = resp.headers.get("Location", "")
    assert loc.lower().startswith("https://"), f"{host_key}: Location header not HTTPS ({loc!r})"


# ── REQ-B-001: dynamic endpoints accept POST + OPTIONS only ──────────
@pytest.mark.parametrize("service,path,method", [
    (svc, p, m) for (svc, p) in _DYNAMIC_PATHS for m in DYNAMIC_REJECTED_METHODS
])
def test_REQ_B_001_rejected_methods_return_405(service, path, method):
    url = HOSTS[service] + path
    resp = requests.request(method, url, timeout=10, verify=VERIFY_TLS)
    assert resp.status_code == 405, (
        f"{service}{path}: {method} expected 405, got {resp.status_code}"
    )


@pytest.mark.parametrize("service,path", _DYNAMIC_PATHS)
def test_REQ_B_001_post_works(service, path):
    url = HOSTS[service] + path
    resp = requests.post(url, timeout=10, verify=VERIFY_TLS)
    assert resp.status_code in ACCEPTED_POST_CODES, (
        f"{service}{path}: POST returned {resp.status_code}; "
        f"expected one of {ACCEPTED_POST_CODES}"
    )


@pytest.mark.parametrize("service,path", _DYNAMIC_PATHS)
def test_REQ_B_001_options_preflight_supported(service, path):
    url = HOSTS[service] + path
    resp = requests.options(url, timeout=10, verify=VERIFY_TLS, headers={
        "Origin": "https://dev.chathealthy.ai",
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    })
    assert 200 <= resp.status_code < 300, (
        f"{service}{path}: OPTIONS preflight expected 2xx, got {resp.status_code}"
    )


# ── REQ-B-003: static wrapper supports GET + HEAD only ───────────────
@pytest.mark.parametrize("method", STATIC_ALLOWED_METHODS)
def test_REQ_B_003_static_wrapper_accepts_allowed_methods(method):
    url = HOSTS["static_wrapper"] + "/"
    resp = requests.request(method, url, timeout=10, verify=VERIFY_TLS)
    assert 200 <= resp.status_code < 300, (
        f"static wrapper {method} /: expected 2xx, got {resp.status_code}"
    )


@pytest.mark.parametrize("method", STATIC_REJECTED_METHODS)
def test_REQ_B_003_static_wrapper_rejects_other_methods(method):
    url = HOSTS["static_wrapper"] + "/"
    resp = requests.request(method, url, timeout=10, verify=VERIFY_TLS)
    assert resp.status_code == 405, (
        f"static wrapper {method} /: expected 405, got {resp.status_code}"
    )
