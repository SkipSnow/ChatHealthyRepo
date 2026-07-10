# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import os
import sys
REQUIRED = ["ENV_PREFIX", "MONGO_connectionString"]
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    print("missing env:", missing)
    sys.exit(1)
print("e2e readiness ok")
