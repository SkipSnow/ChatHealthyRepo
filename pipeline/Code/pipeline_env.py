# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Load .env for local pipeline runs."""

from __future__ import annotations
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import os
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ENV_FILE = _REPO_ROOT / ".env"
_LOADED = False


def load_pipeline_env() -> Path | None:
    """Load .env into os.environ (setdefault — never overrides existing)."""
    global _LOADED
    if _LOADED:
        return _ENV_FILE if _ENV_FILE.is_file() else None
    if _ENV_FILE.is_file():
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))
    _LOADED = True
    return _ENV_FILE if _ENV_FILE.is_file() else None


def require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise ChatHealthyException(mode="runtime_error", message=f"Required environment variable {name!r} is not set")
    return val
