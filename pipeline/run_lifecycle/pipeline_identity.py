# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The pipeline's one Azure credential: pipelineEditor, proven by its client
secret, with no fallback.

Every pipeline component authenticates to Azure as pipelineEditor -- the
application registration whose tenant, client id and client secret the deploy
places in the container environment. Zero trust admits no second identity to
fall back on: if pipelineEditor's credential is not fully present the caller
FAILS, rather than reaching for a managed identity, an ambient credential, or a
DefaultAzureCredential chain that would silently authenticate as something
else. The run host was deliberately stripped of any identity of its own, so a
credential that is not pipelineEditor's is one we did not intend to present.
"""
from __future__ import annotations

import os

from chathealthy_lib.exceptions import ChatHealthyException

_IDENTITY = "pipelineEditor"
_PREFIX = "PIPELINEEDITOR_AZURE"


def pipeline_editor_credential():
    """Return pipelineEditor's ClientSecretCredential, or raise.

    No fallback: a missing tenant, client id or secret is a hard failure, not
    a reason to try another identity.
    """
    tenant = os.environ.get(f"{_PREFIX}_TENANT_ID", "").strip()
    client_id = os.environ.get(f"{_PREFIX}_CLIENT_ID", "").strip()
    secret = os.environ.get(f"{_PREFIX}_CLIENT_SECRET", "").strip()
    missing = [name for name, value in (("tenant_id", tenant),
                                        ("client_id", client_id),
                                        ("client_secret", secret)) if not value]
    if missing:
        raise ChatHealthyException(
            mode="identity_credential_absent",
            component="pipeline_identity",
            message=(f"{_IDENTITY} credential is not present: missing "
                     f"{', '.join(missing)}. The pipeline authenticates as "
                     f"{_IDENTITY} and has no identity to fall back to; the "
                     f"component fails rather than authenticate as anyone "
                     f"else."))
    from azure.identity import ClientSecretCredential  # noqa: PLC0415
    return ClientSecretCredential(
        tenant_id=tenant, client_id=client_id, client_secret=secret)
