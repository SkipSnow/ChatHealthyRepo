# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

def main() -> int:
    root = Path(__file__).parent / "tests"
    cmd = [sys.executable, "-m", "pytest", str(root / "unit"), str(root / "regression"), "-q"]
    return subprocess.call(cmd)

if __name__ == "__main__":
    raise SystemExit(main())
