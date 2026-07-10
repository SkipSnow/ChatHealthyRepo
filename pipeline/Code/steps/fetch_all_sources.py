# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""LLD v22 Provider Pipeline — see pipeline/ArchitectureDesignAndAudit/ProviderPipeline_LowLevelDesign_v22.docx."""

from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


def _crosswalk_already_loaded(env_prefix: str) -> dict | None:
    try:
        from pipeline_db import get_mongo
        coll = get_mongo()[f"{env_prefix}_PublicHealthData"]["ZipCountyCrosswalk"]
        total = coll.estimated_document_count()
        if total > 30000:
            return {"source": "zip_crosswalk", "status": "already_loaded", "total_zips": total}
    except Exception as exc:
        _log.info("crosswalk pre-check skipped: %s", exc)
    return None


def execute(ctx):
    from cluster_lifecycle_manager import ClusterLifecycleManager
    from gather_activities import gather_nppes_zip_activity_fn
    from pipeline_db import get_frontend_mongo

    cluster = ctx.config.get("pipeline_cluster", "ChatHealthyDataPipelines")
    ClusterLifecycleManager(get_db_fn=get_frontend_mongo).wake(cluster, job_id=ctx.run_id)

    cfg = {
        **ctx.config,
        "env_prefix": ctx.env_prefix,
        "states": ctx.state_scope,
        "provider_collection": ctx.provider_collection,
        "embedding_enabled": False,
    }
    crosswalk = _crosswalk_already_loaded(ctx.env_prefix)
    if crosswalk is None:
        from gather_activities import gather_zip_crosswalk_activity_fn
        try:
            crosswalk = gather_zip_crosswalk_activity_fn(cfg)
        except Exception as exc:
            _log.warning("zip_crosswalk load failed, continuing if pre-existing: %s", exc)
            crosswalk = _crosswalk_already_loaded(ctx.env_prefix) or {
                "source": "zip_crosswalk", "status": "load_failed", "error": str(exc)[:300],
            }

    results = {
        "nppes_npi": gather_nppes_zip_activity_fn(cfg),
        "zip_crosswalk": crosswalk,
        "rucc": {"source": "rucc", "status": "deferred_to_urban_flag_step"},
        "catalog": {"source": "specialty_catalog", "status": "skipped_out_of_scope"},
    }
    ctx.manifest.source_versions = {k: "fetched" for k in results}
    return results
