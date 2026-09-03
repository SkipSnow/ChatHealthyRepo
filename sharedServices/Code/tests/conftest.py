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


# The parameters tool validates every write against the declaration the
# runtime binds at startup from ChatHealthyConfig.ParameterDeclaration. A
# unit test runs no startup, so it binds the same declaration from the
# seed the operator loads into that collection. The content is the
# record's, not this file's: a test that carried its own page list would
# be a second declaration.
import json as _json

_DECLARATION_SEED = (
    _REPO_ROOT / "FindCare" / "ParameterDeclaration"
    / "parameter_declaration.json"
)


def pytest_configure(config):  # noqa: ARG001 - pytest hook signature
    from chathealthy_lib import runtime_data_collections as _rdc
    seed = _json.loads(_DECLARATION_SEED.read_text(encoding="utf-8"))
    document = next(d for d in seed["documents"] if d["env"] == "local")
    _rdc._state.env = "local"
    _rdc._state.parameter_declaration = document
