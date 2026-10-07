# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""medicare_source_archival (EPIC-010-F-007) — archive fetched source blobs.

Serial, after fetch. Archival of a fetched source blob plus its DataSourceRegistry
update is a generic concern identical across pipelines, so this delegates to the
shared archival step rather than duplicating it; it archives every source version
recorded on the manifest by medicare_fetch_all_sources (registry name defaults to
the source name for Medicare sources).
"""

from __future__ import annotations

from chathealthy_lib.logging_service import ChatHealthyLoggingService

from pipeline.provider_base.steps.archive_sources import execute as _archive

_log = ChatHealthyLoggingService()


def run_step(ctx) -> dict:
    result = _archive(ctx)
    _log.LogPipeline("INFO", "medicare_source_archival: archived=%s",
                     result.get("count") if isinstance(result, dict) else result)
    return result


def execute(ctx):
    return run_step(ctx)
