# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Which build this process is running.

Named for the fact it carries. It lived inside the health endpoint, which
made a build identity look like a health concern: /health reports it, and
so does the session, and neither of them owns it. A server reporting one
build while executing the code of another is the failure this exists to
make visible, and that is not a question about whether the server is well.

The file is written into the image at build time, so it describes the bytes
that are running rather than anything read at runtime.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()

BUILD_IDENTITY_PATH = "/app/build_info.json"


def build_identity() -> dict[str, Any]:
    """The build, commit and environment baked into this image.

    An empty dict when the image carries none. The caller renders that
    silence; inventing a number would hide exactly the case this answers.
    """
    p = Path(BUILD_IDENTITY_PATH)
    if not p.is_file():
        log.info("no build identity at %s", BUILD_IDENTITY_PATH)
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - converted at this boundary
        log.info(
            "build identity unreadable: %s", exc,
            exc=ChatHealthyException(
                mode="build_identity_unreadable",
                message=f"build identity unreadable at {BUILD_IDENTITY_PATH}: {exc}",
                component="build_identity",
                exception=exc,
            ),
        )
        return {}


def build_number() -> str:
    """The build alone, as a string, or "" when the image does not say."""
    return str(build_identity().get("build") or "")
