# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

import os


class SecretsEndpoint:
    """GET /secrets/{key} — local-mode stub. Returns whether the named secret
    exists in the environment (does not return the secret value itself).
    Production: reads from Azure Key Vault via x509 cert."""

    def __init__(self):
        pass

    def __call__(self, key: str):
        value = os.getenv(key)
        if value:
            return {"key": key, "found": True}
        return {"key": key, "found": False, "error": "Secret not found"}
