# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def write_integration_snapshot(path: Path, payload: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**payload, "captured_at": datetime.now(timezone.utc).isoformat()}
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return str(path)
