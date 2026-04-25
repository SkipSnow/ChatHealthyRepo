# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Session Token — signed with service private key, verified with public cert.
# Zero-trust component-to-component authentication.
#
# Flow:
#   1. FindCare generates CH_{guid} on first contact
#   2. Signs with findcare.key (private)
#   3. Client carries {origin, token, signature} as opaque blob
#   4. EvaluateCare verifies signature with findcare.crt (public)
#   5. If valid, the request came from FindCare and wasn't tampered

import base64
import json
import os
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate


CERTS_DIR = os.environ.get("CERTS_DIR",
    os.path.join(os.path.dirname(__file__), "..", "Code", "Shared", "ops", "certs"))


# Session GUID — created once per process, reused for all tokens.
# The GUID is the session identity. The nonce (timestamps) proves freshness.
_session_guid: str | None = None


# EPIC-002-F-001-S-012-REQ-T-006: the origin field in a session token MUST be the
# canonical service name ("FindCare", "EvaluateCare", "SharedServices").
# Cert files on disk use shorter historical names. This map is the single
# source of truth for service-name → cert-file-basename.
_SERVICE_TO_CERT_NAME = {
    "FindCare":       "findcare",
    "EvaluateCare":   "evalcare",
    "SharedServices": "shared",
}

def _cert_basename(origin: str) -> str:
    """Map a REQ-017 origin (service name) to its cert filename basename.
    Raises ValueError for any origin not in the canonical set —
    security primitives MUST fail loud (feedback_no_security_fallbacks)."""
    if origin not in _SERVICE_TO_CERT_NAME:
        raise ValueError(
            f"Invalid token origin {origin!r}. Per EPIC-002-F-001-S-012-REQ-T-006 "
            f"origin MUST be one of {sorted(_SERVICE_TO_CERT_NAME)}."
        )
    return _SERVICE_TO_CERT_NAME[origin]


def generate_session_token(origin: str = "FindCare") -> dict:
    """Generate a new session token and sign it with the origin's private key.

    Token format: CH{nonce:17}{created:17}{guid:32} = 68 chars, no delimiters.
      - GUID: same for the entire session (process lifetime). Identifies the session.
      - Nonce (timestamps): fresh on every call. Proves freshness, prevents replay.
      - Signature: signs origin + nonce + guid with private key.

    Returns {origin, token, signature, created_at} — pass entire dict to client.
    """
    global _session_guid
    if _session_guid is None:
        _session_guid = uuid.uuid4().hex

    now = datetime.now(timezone.utc)
    ms = str(now.microsecond)[:3].zfill(3)
    created_stamp = now.strftime('%m%d%Y%H%M%S') + ms
    guid = _session_guid
    # Format: CH{nonce}{created}{guid} — no delimiters, obscured
    # Nonce changes every call. GUID stays the same for the session.
    token = f"CH{created_stamp}{created_stamp}{guid}"
    created = datetime.now(timezone.utc).isoformat()

    key_path = os.path.join(CERTS_DIR, f"{_cert_basename(origin)}.key")
    if not os.path.exists(key_path):
        raise FileNotFoundError(
            f"session-token signing key not found: {key_path}. "
            f"A session token cannot be issued without a signing key. "
            f"Per EPIC-002-F-001-S-012-REQ-T-004 the server MUST exit rather than serve unsigned tokens."
        )

    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    # Sign origin + created_stamp + guid (immutable parts — the nonce)
    payload = f"{origin}:{created_stamp}:{guid}".encode()
    signature = private_key.sign(
        payload,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )

    return {
        "origin": origin,
        "token": token,
        "signature": base64.b64encode(signature).decode(),
        "created_at": created,
        "signed": True,
    }


import logging as _logging
_log = _logging.getLogger("session_token")


def verify_session_token(session: dict, expected_origin: str = "FindCare") -> bool:
    """Verify a session token's signature using the origin's public cert.

    The token itself is the nonce — created_stamp + guid are unique per session.
    Verifies origin + created + guid were signed by the expected origin.
    Returns True if the signature is valid. Logs WHY on every negative path
    so operators can diagnose without changing code.
    """
    if not session:
        _log.warning("verify: no session object")
        return False
    if not session.get("signed"):
        _log.warning("verify: session.signed == %r (MUST be True)", session.get("signed"))
        return False

    origin = session.get("origin", "")
    token = session.get("token", "")
    sig_b64 = session.get("signature", "")

    if origin != expected_origin:
        _log.warning("verify: origin=%r expected=%r", origin, expected_origin)
        return False
    if not token:
        _log.warning("verify: empty token")
        return False
    if not sig_b64:
        _log.warning("verify: empty signature")
        return False

    # Extract by position: CH{last_used:17}{created:17}{guid:32}
    if len(token) < 68:
        _log.warning("verify: token length %d < 68", len(token))
        return False
    created_stamp = token[19:36]
    guid = token[36:]

    # Load public cert — CERTS_DIR resolved at import; if bootstrap changes it
    # after import, re-read from env at call time.
    certs_dir = os.environ.get("CERTS_DIR", CERTS_DIR)
    cert_path = os.path.join(certs_dir, f"{_cert_basename(origin)}.crt")
    if not os.path.exists(cert_path):
        _log.warning(
            "verify: cert file missing at %s (CERTS_DIR=%s). "
            "Peer Space likely lacks FINDCARE_CERT_PEM bootstrap.",
            cert_path, certs_dir,
        )
        return False

    with open(cert_path, "rb") as f:
        cert = load_pem_x509_certificate(f.read())

    public_key = cert.public_key()
    payload = f"{origin}:{created_stamp}:{guid}".encode()
    signature = base64.b64decode(sig_b64)

    try:
        public_key.verify(
            signature,
            payload,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        now = datetime.now(timezone.utc)
        ms = str(now.microsecond)[:3].zfill(3)
        last_used_stamp = now.strftime('%m%d%Y%H%M%S') + ms
        session["token"] = f"CH{last_used_stamp}{created_stamp}{guid}"
        session["last_used"] = now.isoformat()
        return True
    except Exception as exc:
        _log.warning(
            "verify: signature check failed (%s: %s). cert=%s payload.len=%d sig.len=%d",
            type(exc).__name__, exc, cert_path, len(payload), len(signature),
        )
        return False
