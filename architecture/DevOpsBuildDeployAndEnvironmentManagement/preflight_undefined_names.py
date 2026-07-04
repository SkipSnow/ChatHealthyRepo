# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Build preflight: reject any commit whose front-end or pipeline Python
carries an unresolved name reference.

Wired into build_chathealthy.py after the crosswalk gate. Uses pyflakes
so the check is fast (AST + name resolution, no imports executed) and
fails the build on the same class of defect (missing import, typo in a
symbol reference) that the smoke test currently catches only at runtime.

Only UndefinedName / UndefinedLocal / UndefinedExport are treated as
build-fatal here. Unused-import, unused-variable, and other lint noise
are out of scope for this preflight.
"""
from __future__ import annotations

import ast
from pathlib import Path

from pyflakes import checker as pf_checker
from pyflakes.messages import UndefinedName, UndefinedLocal, UndefinedExport


# Source roots that produce packaged code. Any Python file under these
# paths goes into a container we ship and MUST resolve every name it uses.
_SCAN_ROOTS = (
    "sharedServices/Code",
    "FindCare",
    "evaluateCare/Code",
    "Code/ConversationalUX/FindCareChat/backend",
    "FrontEndApplicationLib/src/chathealthy_frontend_lib",
    "pipeline/Code",
)

# Paths under a scan root that never ship (throwaway helpers, caches,
# built artifacts, vendored deps). Skipped entirely.
_EXCLUDE_PARTS = frozenset({
    "__pycache__", "_oneshots", "node_modules", "dist", "build",
    "static", ".venv", "archive",
})


def scan(repo_root: Path) -> list[str]:
    """Return one error string per unresolved name. Empty list means clean."""
    errors: list[str] = []
    for r in _SCAN_ROOTS:
        root = repo_root / r
        if not root.exists():
            continue
        for py in root.rglob("*.py"):
            if any(part in _EXCLUDE_PARTS for part in py.parts):
                continue
            try:
                source = py.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source, filename=str(py))
            except SyntaxError as e:
                errors.append(f"{py.relative_to(repo_root).as_posix()}: SyntaxError: {e}")
                continue
            c = pf_checker.Checker(tree, filename=str(py))
            for m in c.messages:
                if isinstance(m, (UndefinedName, UndefinedLocal, UndefinedExport)):
                    errors.append(
                        f"{py.relative_to(repo_root).as_posix()}:{m.lineno}: "
                        f"{m.__class__.__name__}: {m.message % m.message_args}"
                    )
    return errors
