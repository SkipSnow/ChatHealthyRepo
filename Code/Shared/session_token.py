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
from typing import Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.x509 import load_pem_x509_certificate
from pydantic import BaseModel, Field


class SessionToken(BaseModel):
    """Canonical shape returned by every /session endpoint and accepted as
    the input to every /verify-token endpoint. Single source of truth for
    the on-the-wire session-token contract."""
    origin: str = Field(..., description="Issuing service: FindCare | EvaluateCare | SharedServices")
    token: str = Field(..., description="CH{nonce:17}{created:17}{guid:32} — 68 chars, no delimiters")
    signature: str = Field(..., description="Base64-encoded RSA PKCS1v15 SHA256 signature of origin:created:guid")
    created_at: str = Field(..., description="ISO 8601 UTC timestamp the token was minted")
    signed: bool = Field(..., description="True when a signing key was used; False is never returned by a healthy service")
    server_env: Optional[str] = Field(None, description="Issuing container's env (LOCAL|DEV|QA|PROD) — added by the /session endpoint per S-012-REQ-B-010")


class SessionTokenVerification(BaseModel):
    """Canonical shape returned in the `session_token` field of every
    /verify-token response (and any equivalent endpoint that verifies a
    session token, e.g., /evaluate/providers). Audit + verdict — distinct
    from SessionToken, which is the on-the-wire input shape."""
    token_received: str = Field(..., description="Echo of the token string received from the caller")
    signature_received: str = Field(..., description="First 40 chars of the received signature, then '...' — for visual audit, not re-verification")
    origin: str = Field(..., description="Self-identification of the responding service: FindCare | EvaluateCare | SharedServices (per S-012-REQ-B-010, distinct from the cryptographic signer)")
    verified: bool = Field(..., description="True if the signature validated against the named origin's public cert")


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


class TokenInfraError(RuntimeError):
    """Raised when verify_session_token cannot complete because of an
    infrastructure / configuration failure (missing cert file, unreadable
    CERTS_DIR, malformed cert, etc.). Distinct from a legitimate
    `verified == False` outcome (signature didn't match a present cert).
    Callers should let this propagate so the operator sees a 500 +
    visible Fatal Error rather than a misleading verification verdict."""


def verify_session_token(session: dict, expected_origin: str = "FindCare") -> bool:
    """Verify a session token's signature using the origin's public cert.

    Returns:
        True  — signature matches; receiver-side last_used has been mutated
                into the session dict per EPIC-002-F-001-S-012-REQ-T-003.
        False — token is well-formed and the signature was checked against
                a present cert and did not match. THIS IS THE ONLY MEANING
                OF False. A False return is NOT a fallback for "the cert
                file was missing" or "the request shape was bad" — those
                conditions raise TokenInfraError or ValueError instead.

    Raises:
        ValueError       — required token field missing/malformed (caller
                           sent garbage; legitimate 4xx).
        TokenInfraError  — server cannot complete verification (cert file
                           missing, CERTS_DIR unreadable, malformed cert
                           on disk). Operator infra fix needed; legitimate
                           5xx that must propagate (no fallback).
    """
    if not session:
        raise ValueError("verify: no session object")
    if not session.get("signed"):
        raise ValueError(f"verify: session.signed == {session.get('signed')!r} (MUST be True)")

    origin = session.get("origin", "")
    token = session.get("token", "")
    sig_b64 = session.get("signature", "")

    if origin != expected_origin:
        raise ValueError(f"verify: origin={origin!r} expected={expected_origin!r}")
    if not token:
        raise ValueError("verify: empty token")
    if not sig_b64:
        raise ValueError("verify: empty signature")
    if len(token) < 68:
        raise ValueError(f"verify: token length {len(token)} < 68")

    # Extract by position: CH{last_used:17}{created:17}{guid:32}
    created_stamp = token[19:36]
    guid = token[36:]

    # Load public cert. Cert-related failures are infrastructure; raise so
    # the operator sees them rather than a `verified=False` impostor.
    certs_dir = os.environ.get("CERTS_DIR", CERTS_DIR)
    cert_path = os.path.join(certs_dir, f"{_cert_basename(origin)}.crt")
    if not os.path.exists(cert_path):
        raise TokenInfraError(
            f"cert file missing at {cert_path} (CERTS_DIR={certs_dir}). "
            f"Peer Space likely lacks FINDCARE_CERT_PEM bootstrap."
        )
    try:
        with open(cert_path, "rb") as f:
            cert_pem = f.read()
        cert = load_pem_x509_certificate(cert_pem)
    except Exception as exc:
        raise TokenInfraError(
            f"failed to load cert at {cert_path}: {type(exc).__name__}: {exc}"
        ) from exc

    public_key = cert.public_key()
    payload = f"{origin}:{created_stamp}:{guid}".encode()
    try:
        signature = base64.b64decode(sig_b64)
    except Exception as exc:
        raise ValueError(
            f"verify: signature is not valid base64: {type(exc).__name__}: {exc}"
        ) from exc

    # The actual cryptographic check. InvalidSignature is the ONLY path
    # that legitimately yields `verified == False`; anything else is infra.
    from cryptography.exceptions import InvalidSignature
    try:
        public_key.verify(signature, payload, padding.PKCS1v15(), hashes.SHA256())
    except InvalidSignature:
        _log.warning(
            "verify: InvalidSignature for origin=%s cert=%s",
            origin, cert_path,
        )
        return False
    except Exception as exc:
        # cryptography lib internal errors (corrupted key, etc.) — infra.
        raise TokenInfraError(
            f"crypto.verify raised {type(exc).__name__}: {exc}"
        ) from exc

    # Signature good — receiver MUST update last_used per REQ-T-003.
    now = datetime.now(timezone.utc)
    ms = str(now.microsecond)[:3].zfill(3)
    last_used_stamp = now.strftime('%m%d%Y%H%M%S') + ms
    session["token"] = f"CH{last_used_stamp}{created_stamp}{guid}"
    session["last_used"] = now.isoformat()
    return True
