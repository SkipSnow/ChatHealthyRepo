# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Pytest conftest for sharedServices/Code tests.

Puts sharedServices/Code on sys.path so test modules can import the
same packages app.py imports at runtime (authentication, UtteranceManager,
CloseConnection200Tool, etc.). Also puts FindCare on
sys.path so SpecialtyFilter is importable when tests need it.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHARED_CODE = _REPO_ROOT / "sharedServices" / "Code"
_FRONTEND_LIB = _REPO_ROOT / "ChatHealthyLib" / "src"
_FINDCARE = _REPO_ROOT / "FindCare"

for p in (str(_SHARED_CODE), str(_FRONTEND_LIB), str(_FINDCARE)):
    if p not in sys.path:
        sys.path.insert(0, p)

# AuthorizationsAndAuthentications package is mapped to "authentication"
# in the container Dockerfile via COPY dst rename. Provide the same
# alias in tests so imports match the runtime module path.
_AA = _SHARED_CODE / "AuthorizationsAndAuthentications"
import importlib
import importlib.util

if "authentication" not in sys.modules:
    spec = importlib.util.spec_from_file_location(
        "authentication",
        str(_AA / "__init__.py"),
        submodule_search_locations=[str(_AA)],
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    sys.modules["authentication"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
