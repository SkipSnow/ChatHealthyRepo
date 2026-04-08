# Copyright (c) 2026 Skip Snow. All rights reserved.
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


def generate_session_token(origin: str = "FindCare") -> dict:
    """Generate a new session token and sign it with the origin's private key.

    Returns {origin, token, signature, created_at} — pass entire dict to client.
    """
    token = f"CH_{uuid.uuid4().hex}"
    created = datetime.now(timezone.utc).isoformat()

    # Load private key for signing
    key_path = os.path.join(CERTS_DIR, f"{origin.lower()}.key")
    if not os.path.exists(key_path):
        # No cert — return unsigned (local dev without certs)
        return {
            "origin": origin,
            "token": token,
            "signature": None,
            "created_at": created,
            "signed": False,
        }

    with open(key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)

    # Sign the token + origin + timestamp
    payload = f"{origin}:{token}:{created}".encode()
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


def verify_session_token(session: dict, expected_origin: str = "FindCare") -> bool:
    """Verify a session token's signature using the origin's public cert.

    Returns True if the token was signed by the expected origin.
    """
    if not session or not session.get("signed"):
        return False

    origin = session.get("origin", "")
    token = session.get("token", "")
    sig_b64 = session.get("signature", "")
    created = session.get("created_at", "")

    if origin != expected_origin or not token or not sig_b64:
        return False

    # Load public cert
    cert_path = os.path.join(CERTS_DIR, f"{origin.lower()}.crt")
    if not os.path.exists(cert_path):
        return False

    with open(cert_path, "rb") as f:
        cert = load_pem_x509_certificate(f.read())

    public_key = cert.public_key()
    payload = f"{origin}:{token}:{created}".encode()
    signature = base64.b64decode(sig_b64)

    try:
        public_key.verify(
            signature,
            payload,
            padding.PKCS1v15(),
            hashes.SHA256(),
        )
        return True
    except Exception:
        return False
