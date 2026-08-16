# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""prepare_infrastructure — wake Atlas, reserve cluster, ensure indexes."""

from __future__ import annotations

import time
from chathealthy_lib.logging_service import ChatHealthyLoggingService


from cluster_lifecycle_manager import ClusterLifecycleManager
from ensure_provider_indexes_activity import ensure_provider_indexes_fn
from pipeline_config import ensure_pipeline_config
from pipeline_db import get_mongo

_log = ChatHealthyLoggingService()


_PIPELINE_OWNED_STAGING_TO_RESET = [
    # Collections that the pipeline builds fresh every run for its own
    # scoped-state slice. Provider_v_3 is deliberately NOT here -
    # provider_normalize_engine's state-scoped delete_many owns that.
    ("PublicStaging", "OtherIdentifierPhrases"),
]


def _safety_cleanup(mongo, scoped_states: list[str]) -> dict:
    """Best-effort reset of pipeline-owned staging collections that a
    prior run may have left populated in the current state scope.

    Runs at pipeline initialization so production re-fires do not need
    any manual intervention. If a collection or database does not
    exist yet (first-fire scenario), the delete is a no-op - do NOT
    raise.

    Only touches collections in _PIPELINE_OWNED_STAGING_TO_RESET.
    Target Provider_v_3 collection is explicitly OFF-LIMITS - normalize
    handles its own state-scoped drain.
    """
    upper_states = [s.upper().strip() for s in (scoped_states or []) if s]
    if not upper_states:
        # No silent full-wipe: prior code would delete_many({}) the entire
        # cross-fire collection when caller passed empty state scope. That
        # kills work for OTHER in-flight fires. Caller MUST pass an
        # explicit state scope; otherwise refuse to touch anything.
        from chathealthy_lib.exceptions import ChatHealthyException
        raise ChatHealthyException(
            mode="prepare_infrastructure_missing_state_scope",
            message=(
                "prepare_infrastructure safety-cleanup: scoped_states is empty. "
                "Silent full-wipe of the pipeline-owned staging collections is "
                "forbidden -- caller MUST pass an explicit list of state codes."
            ),
        )
    results = []
    for db_name, coll_name in _PIPELINE_OWNED_STAGING_TO_RESET:
        try:
            coll = mongo[db_name][coll_name]
            n = coll.delete_many({"state": {"$in": upper_states}}).deleted_count
            scope_note = f"state in {upper_states}"
            results.append({
                "collection": f"{db_name}.{coll_name}",
                "deleted": n,
                "scope": scope_note,
            })
        except Exception as exc:
            # Missing collection / missing DB / no-permissions edge case
            # is tolerable at init - the next step will populate anyway.
            results.append({
                "collection": f"{db_name}.{coll_name}",
                "deleted": 0,
                "skipped_reason": f"{type(exc).__name__}: {exc}",
            })
    return {"safety_cleanup": results}


def execute(ctx) -> dict:
    """Wrapper: the step says it started, and says why it stopped.

    A phase that raised said nothing, so a step that died mid-way looked
    exactly like one still working, and the run status kept reading
    "not started" because nothing recorded that it had begun.
    """
    started = time.time()
    _log.info("prepare_infrastructure: STEP START run_id=%s", ctx.run_id)
    try:
        result = _execute(ctx)
    except Exception as exc:
        _log.error("prepare_infrastructure: STEP FAILED after %.1fs run_id=%s "
                   "%s: %s", time.time() - started, ctx.run_id,
                   type(exc).__name__, exc)
        raise
    _log.info("prepare_infrastructure: STEP DONE in %.1fs run_id=%s",
              time.time() - started, ctx.run_id)
    return result


def _execute(ctx) -> dict:
    # Every call below can block for minutes -- waking Atlas, reserving it,
    # and an index check that waits out a paused cluster. Unbracketed, the
    # worker went silent here for the whole wait and the Controller could
    # only report "remaining=1". Each phase now says it started and what it
    # cost, so a stall names itself.
    # Operator directive 2026-08-03: coord on pipeline cluster only.
    _log.info("prepare_infrastructure: begin run_id=%s states=%s",
              ctx.run_id, list(ctx.args.resolved_states() or []))
    t0 = time.time()
    cfg = ensure_pipeline_config(get_mongo(), ctx.env_prefix)
    _log.info("prepare_infrastructure: config read (%.1fs) sources=%d",
              time.time() - t0, len(cfg.get("dataset_versions") or []))
    ctx.config.setdefault("dataset_versions", cfg.get("dataset_versions", {}))
    ctx.config.setdefault("source_freshness", cfg.get("source_freshness", []))
    cluster = ctx.config.get("pipeline_cluster", "ChatHealthyDataPipelines")
    duration = int(ctx.args.expected_duration_minutes)
    ops = ClusterLifecycleManager(get_db_fn=get_mongo)

    t = time.time()
    _log.info("prepare_infrastructure: waking cluster %s", cluster)
    ops.wake(cluster, job_id=ctx.run_id)
    _log.info("prepare_infrastructure: cluster %s awake (%.1fs)",
              cluster, time.time() - t)

    t = time.time()
    _log.info("prepare_infrastructure: reserving %s for %d minute(s)",
              cluster, duration)
    reservation = ops.reserve(
        cluster_name=cluster,
        job_id=ctx.run_id,
        requester="provider_pipeline_lld",
        expected_duration_minutes=duration,
    )
    _log.info("prepare_infrastructure: reserved (%.1fs)", time.time() - t)

    t = time.time()
    wait_minutes = int(ctx.config.get("cluster_wait_minutes", 20))
    _log.info("prepare_infrastructure: ensuring indexes on %s "
              "(waits up to %d minute(s) for the cluster)",
              ctx.provider_collection, wait_minutes)
    idx = ensure_provider_indexes_fn({
        "provider_collection": ctx.provider_collection,
        "cluster_wait_minutes": wait_minutes,
    })
    _log.info("prepare_infrastructure: indexes ready (%.1fs)", time.time() - t)

    t = time.time()
    cleanup = _safety_cleanup(
        ctx.mongo_client or get_mongo(),
        list(ctx.args.resolved_states() or []),
    )
    _log.info("prepare_infrastructure: safety cleanup done (%.1fs); "
              "step complete in %.1fs", time.time() - t, time.time() - t0)
    ctx.manifest.metrics["reservation"] = reservation
    ctx.manifest.metrics["safety_cleanup"] = cleanup
    return {"cluster_ready": True, "indexes": idx, "reservation": reservation, **cleanup}
