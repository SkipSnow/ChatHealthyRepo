# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""SyncGatewayAgent tests — CopyToFrontEnd v2."""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync_gateway_agent import SyncGatewayAgent, PromotionBlockedError, COPY_BATCH_SIZE


def test_batch_size_is_500():
    """Memory safety: batch size must be 500 to stay under memory budget."""
    assert COPY_BATCH_SIZE == 500, f"COPY_BATCH_SIZE is {COPY_BATCH_SIZE}, must be 500"


def test_static_collections_list():
    """All required static collections must be in the sync list."""
    from sync_gateway_agent import STATIC_COLLECTIONS
    required = {"SpecialtyMetaData", "provider_quality", "ICD10Codes",
                "ZipCountyCrosswalk", "drug_crosswalk_cache"}
    assert required == set(STATIC_COLLECTIONS), \
        f"Missing static collections: {required - set(STATIC_COLLECTIONS)}"


def test_large_state_threshold():
    """States over threshold must be split by _id range."""
    from sync_gateway_agent import LARGE_STATE_THRESHOLD
    assert LARGE_STATE_THRESHOLD == 200_000


def test_sync_gateway_has_atomic_swap():
    """SyncGatewayAgent must have atomic_swap method."""
    assert hasattr(SyncGatewayAgent, "atomic_swap")


def test_sync_gateway_has_gate_checks():
    """SyncGatewayAgent must have run_gate_checks method."""
    assert hasattr(SyncGatewayAgent, "run_gate_checks")


def test_sync_gateway_has_recover_orphans():
    """SyncGatewayAgent must have recover_orphans for idempotent resume."""
    assert hasattr(SyncGatewayAgent, "recover_orphans")


def test_sync_gateway_has_stream_to_temp():
    """SyncGatewayAgent must have stream_to_temp method."""
    assert hasattr(SyncGatewayAgent, "stream_to_temp")


def test_promotion_blocked_error_exists():
    """PromotionBlockedError must carry checks dict."""
    err = PromotionBlockedError("test", {"check": "failed"})
    assert err.checks == {"check": "failed"}


def test_no_readall_in_sync_gateway():
    """DR-024: sync_gateway_agent.py must not contain .readall()."""
    fpath = os.path.join(os.path.dirname(__file__), "..", "sync_gateway_agent.py")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert ".readall()" not in content


def test_sync_lock_pattern():
    """SyncGatewayAgent must use admin.SyncLock to prevent concurrent syncs."""
    fpath = os.path.join(os.path.dirname(__file__), "..", "sync_gateway_agent.py")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "SyncLock" in content


def test_promotion_log_pattern():
    """SyncGatewayAgent must log to admin.PromotionLog."""
    fpath = os.path.join(os.path.dirname(__file__), "..", "sync_gateway_agent.py")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert "PromotionLog" in content


def test_atomic_swap_uses_rename():
    """Atomic swap must use MongoDB rename, not drop+insert."""
    fpath = os.path.join(os.path.dirname(__file__), "..", "sync_gateway_agent.py")
    with open(fpath, "r", encoding="utf-8") as f:
        content = f.read()
    assert ".rename(" in content
    assert "_backup_" in content
    assert "_tmp_sync_" in content
