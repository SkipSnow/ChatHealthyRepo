# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""atlas_cluster_manager.py — scale Atlas clusters up/down via the Atlas API.

Usage (CLI):
    python atlas_cluster_manager.py scale-up   ChatHealthyDataPipelines
    python atlas_cluster_manager.py scale-down ChatHealthyDataPipelines

Scales up to JOB_TIER (M30) with JOB_MAX (M200) ceiling before heavy jobs.
Scales back down to IDLE_TIER (M10) with IDLE_MAX (M20) ceiling after jobs.
Waits for IDLE before returning so callers can fire work immediately after.

Used by migrate_to_new_cluster.py and can be called from any pipeline script.
"""

import logging
import os
import time
from pathlib import Path

import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

ATLAS_BASE    = "https://cloud.mongodb.com/api/atlas/v2"
PUBLIC_KEY    = os.environ["ATLAS_PUBLIC_KEY"]
PRIVATE_KEY   = os.environ["ATLAS_PRIVATE_KEY"]
PROJECT_ID    = os.environ["ATLAS_PROJECT_ID"]

# Tier config
JOB_TIER      = "M30"    # pre-scale before heavy jobs
JOB_MAX       = "M200"   # autoscale ceiling during jobs
IDLE_TIER     = "M10"    # base tier when idle
IDLE_MAX      = "M20"    # autoscale ceiling when idle

POLL_INTERVAL = 15       # seconds between state checks
TIMEOUT_MIN   = 30       # give up after this many minutes


def _auth():
    return HTTPDigestAuth(PUBLIC_KEY, PRIVATE_KEY)


def _headers():
    return {"Accept": "application/vnd.atlas.2023-02-01+json",
            "Content-Type": "application/json"}


TIER_ORDER = ["M10", "M20", "M30", "M40", "M50", "M60", "M80", "M140", "M200"]


def _tier_index(tier: str) -> int:
    try:
        return TIER_ORDER.index(tier)
    except ValueError:
        return -1


def get_cluster_info(cluster_name: str) -> dict:
    """Return {'state': str, 'tier': str} for the cluster."""
    r = requests.get(
        f"{ATLAS_BASE}/groups/{PROJECT_ID}/clusters/{cluster_name}",
        auth=_auth(), headers=_headers(), timeout=30
    )
    r.raise_for_status()
    data = r.json()
    state = data.get("stateName", "UNKNOWN")
    try:
        tier = data["replicationSpecs"][0]["regionConfigs"][0]["electableSpecs"]["instanceSize"]
    except (KeyError, IndexError):
        tier = "UNKNOWN"
    return {"state": state, "tier": tier}


def get_cluster_state(cluster_name: str) -> str:
    return get_cluster_info(cluster_name)["state"]


def resize_cluster(cluster_name: str, instance_size: str, max_size: str) -> None:
    """Submit a resize request. Does not wait for completion."""
    payload = {
        "replicationSpecs": [{
            "regionConfigs": [{
                "providerName": "AZURE",
                "regionName": "US_EAST_2",
                "priority": 7,
                "electableSpecs": {"instanceSize": instance_size, "nodeCount": 3},
                "autoScaling": {
                    "compute": {
                        "enabled": True,
                        "scaleDownEnabled": True,
                        "minInstanceSize": IDLE_TIER,
                        "maxInstanceSize": max_size,
                    },
                    "diskGB": {"enabled": True},
                },
            }]
        }]
    }
    r = requests.patch(
        f"{ATLAS_BASE}/groups/{PROJECT_ID}/clusters/{cluster_name}",
        auth=_auth(), headers=_headers(), json=payload, timeout=30
    )
    if r.status_code not in (200, 202):
        log.error("Resize failed: %s %s", r.status_code, r.text)
        r.raise_for_status()
    log.info("Resize to %s requested (max %s). Cluster entering UPDATING.", instance_size, max_size)


def wait_for_idle(cluster_name: str) -> None:
    deadline = time.time() + TIMEOUT_MIN * 60
    while time.time() < deadline:
        state = get_cluster_state(cluster_name)
        log.info("%s state: %s", cluster_name, state)
        if state == "IDLE":
            return
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{cluster_name} did not reach IDLE within {TIMEOUT_MIN} minutes")


def scale_up(cluster_name: str) -> None:
    log.info("Scaling UP %s → %s (max %s)", cluster_name, JOB_TIER, JOB_MAX)
    info = get_cluster_info(cluster_name)
    if info["state"] != "IDLE":
        log.info("Cluster is %s — waiting for IDLE before resizing...", info["state"])
        wait_for_idle(cluster_name)
        info = get_cluster_info(cluster_name)

    if _tier_index(info["tier"]) >= _tier_index(JOB_TIER):
        log.info("%s already at %s — no resize needed.", cluster_name, info["tier"])
        return

    resize_cluster(cluster_name, JOB_TIER, JOB_MAX)
    wait_for_idle(cluster_name)
    log.info("%s is ready at %s.", cluster_name, JOB_TIER)


def scale_down(cluster_name: str) -> None:
    log.info("Scaling DOWN %s → %s (max %s)", cluster_name, IDLE_TIER, IDLE_MAX)
    state = get_cluster_state(cluster_name)
    if state != "IDLE":
        log.info("Cluster is %s — waiting for IDLE before resizing...", state)
        wait_for_idle(cluster_name)
    resize_cluster(cluster_name, IDLE_TIER, IDLE_MAX)
    log.info("Scale-down submitted. Cluster will resize in background.")


if __name__ == "__main__":
    import sys
    if len(sys.argv) != 3 or sys.argv[1] not in ("scale-up", "scale-down"):
        print("Usage: python atlas_cluster_manager.py <scale-up|scale-down> <cluster-name>")
        sys.exit(1)
    action, name = sys.argv[1], sys.argv[2]
    if action == "scale-up":
        scale_up(name)
    else:
        scale_down(name)
