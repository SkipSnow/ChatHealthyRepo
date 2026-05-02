# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os


class DebugBootstrapEndpoint:
    """GET /debug/bootstrap — back door for mTLS/cert audit.
    Reports whether the HF Space Secret bootstrap wrote findcare.crt + peer
    certs into CERTS_DIR. Gated by DEBUG=1."""

    def __init__(self):
        self.enabled = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

    def __call__(self):
        if not self.enabled:
            return {"disabled": "set DEBUG=1 on the Space to enable this endpoint"}
        certs_dir = os.environ.get("CERTS_DIR", "<unset>")
        files = []
        if os.path.isdir(certs_dir):
            for f in sorted(os.listdir(certs_dir)):
                files.append({"name": f, "size": os.path.getsize(os.path.join(certs_dir, f))})
        return {
            "CERTS_DIR": certs_dir,
            "certs_dir_files": files,
            "env_present": {
                "FINDCARE_CERT_PEM":        bool(os.environ.get("FINDCARE_CERT_PEM")),
                "EVALCARE_CERT_PEM":        bool(os.environ.get("EVALCARE_CERT_PEM")),
                "EVALCARE_SIGNING_KEY_PEM": bool(os.environ.get("EVALCARE_SIGNING_KEY_PEM")),
                "CA_CERT_PEM":              bool(os.environ.get("CA_CERT_PEM")),
            },
        }
