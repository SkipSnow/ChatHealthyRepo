"""Pytest configuration — make the code package importable."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
_FEATURE_ROOT = _TESTS_DIR.parent
_CODE_DIR = _FEATURE_ROOT / "code"
_REPO_ROOT = _FEATURE_ROOT.parent.parent

# Make `code/` importable as a flat module (chathealthy_enforcement_manager,
# enforcement_worker, scan_files_enforcement_worker).
sys.path.insert(0, str(_CODE_DIR))

# Make the feature root importable so subprocess invocations can resolve
# locks/ etc. relative to PROJECT_ROOT.
sys.path.insert(0, str(_REPO_ROOT))
