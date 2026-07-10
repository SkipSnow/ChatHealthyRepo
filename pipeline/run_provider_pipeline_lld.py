# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

#!/usr/bin/env python3
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "Code"
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

from control_runner import main

if __name__ == "__main__":
    raise SystemExit(main())
