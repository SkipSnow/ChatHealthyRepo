# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pause or resume a MongoDB Atlas cluster.
# Usage:
#   python atlas_cluster_toggle.py <cluster_name> --pause
#   python atlas_cluster_toggle.py <cluster_name> --resume
#   python atlas_cluster_toggle.py ChatHealthyDataPipelines --pause

import os
import sys
import requests
from requests.auth import HTTPDigestAuth
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


def toggle_cluster(cluster_name: str, pause: bool):
    public_key = os.getenv("ATLAS_PUBLIC_KEY")
    private_key = os.getenv("ATLAS_PRIVATE_KEY")
    project_id = os.getenv("ATLAS_PROJECT_ID")

    if not all([public_key, private_key, project_id]):
        print("ERROR: ATLAS_PUBLIC_KEY, ATLAS_PRIVATE_KEY, or ATLAS_PROJECT_ID not set")
        sys.exit(1)

    url = f"https://cloud.mongodb.com/api/atlas/v2/groups/{project_id}/clusters/{cluster_name}"
    auth = HTTPDigestAuth(public_key, private_key)
    headers = {
        "Accept": "application/vnd.atlas.2023-02-01+json",
        "Content-Type": "application/json",
    }

    # Check current state
    resp = requests.get(url, auth=auth, headers=headers, timeout=15)
    if resp.status_code != 200:
        print(f"ERROR: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)

    data = resp.json()
    current_state = data["stateName"]
    current_paused = data.get("paused", False)
    print(f"Cluster: {cluster_name}")
    print(f"  Current: {current_state} (paused={current_paused})")

    if pause and current_paused:
        print("  Already paused. No action.")
        return
    if not pause and not current_paused:
        print("  Already running. No action.")
        return

    # Toggle
    action = "Pausing" if pause else "Resuming"
    print(f"  {action}...")
    resp = requests.patch(url, auth=auth, headers=headers, json={"paused": pause}, timeout=15)
    if resp.status_code == 200:
        print(f"  {action} initiated.")
    else:
        print(f"  ERROR: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 3 or sys.argv[2] not in ("--pause", "--resume"):
        print("Usage: python atlas_cluster_toggle.py <cluster_name> --pause|--resume")
        sys.exit(1)
    toggle_cluster(sys.argv[1], sys.argv[2] == "--pause")
