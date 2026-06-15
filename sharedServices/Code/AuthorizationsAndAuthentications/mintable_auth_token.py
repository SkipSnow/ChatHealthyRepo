# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import base64
from chathealthy_frontend_lib import ChatHealthyLoggingService
import os
import uuid
from datetime import datetime, timezone

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from chathealthy_frontend_lib.authentication.auth_token import AuthToken
from chathealthy_frontend_lib.authentication.nonce import Nonce
from chathealthy_frontend_lib.authentication.session_token import (
    CERTS_DIR,
    SessionToken,
    _cert_basename,
)

log = ChatHealthyLoggingService()


TOKEN_PREFIX = "CH"
ORIGIN = "SharedServices"


class MintableAuthToken(AuthToken):
    @classmethod
    def manufacture(cls, server_env: str) -> AuthToken:
        guid = uuid.uuid4().hex
        nonce_field = Nonce.fresh()
        original_stamp = Nonce.original_stamp(nonce_field)

        sig_b64 = cls._sign(origin=ORIGIN, original_stamp=original_stamp, guid=guid)
        token_str = f"{_TOKEN_PREFIX}{nonce_field}{guid}"

        st = SessionToken(
            origin=ORIGIN,
            token=token_str,
            signature=sig_b64,
            created_at=datetime.now(timezone.utc).isoformat(),
            signed=True,
            server_env=server_env,
        )
        log.info("Manufactured AuthToken: env=%s guid_prefix=%s", server_env, guid[:8])
        return cls(st, origin=ORIGIN)

    @staticmethod
    def _sign(origin: str, original_stamp: str, guid: str) -> str:
        certs_dir = os.environ.get("CERTS_DIR", CERTS_DIR)
        key_path = os.path.join(certs_dir, f"{_cert_basename(origin)}.key")
        if not os.path.exists(key_path):
            raise FileNotFoundError(f"signing key not found: {key_path}")
        with open(key_path, "rb") as f:
            private_key = serialization.load_pem_private_key(f.read(), password=None)
        payload = f"{origin}:{original_stamp}:{guid}".encode()
        sig_bytes = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
        return base64.b64encode(sig_bytes).decode()
