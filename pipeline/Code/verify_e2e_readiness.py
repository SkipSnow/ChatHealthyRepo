# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import os
import sys
REQUIRED = ["ENV_PREFIX"]
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    ChatHealthyLoggingService().info("missing env:", missing)
    sys.exit(1)
ChatHealthyLoggingService().info("e2e readiness ok")
