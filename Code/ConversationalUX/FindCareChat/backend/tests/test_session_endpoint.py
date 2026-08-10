# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# EPIC-009-F-001-S-004-REQ-B-001:
#   "GET /session must return a signed session token or throw an error.
#    Each service MUST be able to set the origin of the token, as they
#    update the NONCE and are the origin of the signed token."
#
# One pytest class, one origin variable, three values.
# Usage: pytest test_session_endpoint.py -v

import base64
import os
import shutil

import pytest
import requests
import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / '.git').exists():
        _lib = _d / 'FrontEndApplicationLib' / 'src'
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.exceptions import ChatHealthyException


_BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
_CERTS_DIR = os.path.join(_BASE_DIR, "Code", "Shared", "ops", "certs")


def _config(origin: str):
    """Map an origin name to its endpoint URL and client-cert pair (or None)."""
    if origin == "FindCare":
        return ("https://localhost:8080/session", None)
    if origin == "EvaluateCare":
        return (
            "https://localhost:8081/session",
            (os.path.join(_CERTS_DIR, "findcare.crt"),
             os.path.join(_CERTS_DIR, "findcare.key")),
        )
    if origin == "SharedServices":
        return (
            "https://localhost:8082/session",
            (os.path.join(_CERTS_DIR, "shared.crt"),
             os.path.join(_CERTS_DIR, "shared.key")),
        )
    raise ChatHealthyException(
        mode="value_error",
        component="test_session_endpoint",
        message=f"unknown origin {origin!r}")


def _server_key_path(origin: str) -> str:
    """Path to the signing key the server uses to sign tokens it issues."""
    return os.path.join(_CERTS_DIR, {
        "FindCare":       "findcare.key",
        "EvaluateCare":   "evalcare.key",
        "SharedServices": "shared.key",
    }[origin])


def _post_session(origin: str):
    url, cert = _config(origin)
    return requests.post(url, cert=cert, verify=False, timeout=10)


class TestSessionEndpoint:
    """One pytest class, one `origin` arg, three values. Each test runs three
    times — once per origin — and asserts the REQ-B-001 contract for that
    service's `/session` endpoint."""

    @pytest.fixture(params=["FindCare", "EvaluateCare", "SharedServices"])
    def origin(self, request):
        return request.param

    def _skip_if_down(self, exc: Exception, origin: str):
        pytest.skip(f"{origin} not running: {type(exc).__name__}: {exc}")

    def test_returns_signed_token(self, origin):
        try:
            resp = _post_session(origin)
        except Exception as e:
            self._skip_if_down(e, origin); return
        assert resp.status_code == 200, f"{origin} /session returned {resp.status_code}: {resp.text[:200]}"
        body = resp.json()
        assert body.get("signed") is True, f"{origin} /session returned signed={body.get('signed')!r}"
        assert body.get("signature"), f"{origin} /session returned empty signature"

    def test_origin_field_matches_calling_service(self, origin):
        try:
            resp = _post_session(origin)
        except Exception as e:
            self._skip_if_down(e, origin); return
        assert resp.status_code == 200
        body = resp.json()
        assert body.get("origin") == origin, \
            f"{origin} /session returned origin={body.get('origin')!r}, expected {origin!r}"

    def test_token_shape(self, origin):
        try:
            resp = _post_session(origin)
        except Exception as e:
            self._skip_if_down(e, origin); return
        assert resp.status_code == 200
        body = resp.json()
        tok = body.get("token") or ""
        assert tok.startswith("CH"), f"{origin} token missing CH prefix: {tok!r}"
        assert len(tok) >= 69, f"{origin} token length {len(tok)} < 69 (new wire format: CH + 35-byte nonce + 32-char GUID)"

    def test_signature_is_base64_rsa_shaped(self, origin):
        try:
            resp = _post_session(origin)
        except Exception as e:
            self._skip_if_down(e, origin); return
        assert resp.status_code == 200
        sig_b64 = resp.json().get("signature") or ""
        try:
            raw = base64.b64decode(sig_b64, validate=True)
        except Exception as e:
            pytest.fail(f"{origin} signature is not valid base64: {e}")
        # RSA-2048 PKCS1v15 SHA256 → 256-byte signature. Tolerate 2048/3072/4096.
        assert len(raw) in (256, 384, 512), \
            f"{origin} signature length {len(raw)} is not a plausible RSA modulus size"

    def test_throws_when_signing_key_missing(self, origin):
        """REQ-B-001: '...or throw an error.' Move the signing key aside and
        confirm /session does NOT return 200 with signed=False — it must
        propagate as a 5xx (no silent unsigned fallback)."""
        key_path = _server_key_path(origin)
        if not os.path.exists(key_path):
            pytest.skip(f"{origin} signing key not present at {key_path}; cannot exercise miss path")
        backup = key_path + ".pytest_backup"
        shutil.move(key_path, backup)
        try:
            try:
                resp = _post_session(origin)
            except Exception as e:
                self._skip_if_down(e, origin); return
            assert resp.status_code >= 500, \
                f"{origin} /session must throw 5xx when key missing, got {resp.status_code}: {resp.text[:200]}"
            body_text = resp.text.lower()
            assert "signed" not in body_text or '"signed":true' not in body_text.replace(" ", ""), \
                f"{origin} /session returned a token despite missing key: {resp.text[:200]}"
        finally:
            shutil.move(backup, key_path)
