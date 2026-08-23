# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import os
import sys
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
REQUIRED = ["ENV_PREFIX"]
missing = [k for k in REQUIRED if not os.environ.get(k)]
if missing:
    ChatHealthyLoggingService().info("missing env:", missing)
    raise ChatHealthyException(
        mode="aborted",
        component="verify_e2e_readiness",
        message=1)
ChatHealthyLoggingService().info("e2e readiness ok")
