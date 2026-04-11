# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Check HuggingFace Space status.
# Usage:
#   python hf_space_status.py <space_name>
#   python hf_space_status.py dev_ChatHealthyAIChatWindow

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


def check_status(space_name: str):
    token = os.getenv("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN not set")
        sys.exit(1)

    # Space API status
    url = f"https://huggingface.co/api/spaces/SkipSnow/{space_name}"
    resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code != 200:
        print(f"ERROR: {resp.status_code}")
        sys.exit(1)

    data = resp.json()
    runtime = data.get("runtime", {})
    print(f"Space: SkipSnow/{space_name}")
    print(f"  Stage: {runtime.get('stage')}")
    print(f"  Hardware: {runtime.get('hardware')}")
    print(f"  Repo SHA: {data.get('sha', '')[:12]}")
    print(f"  Runtime SHA: {runtime.get('sha', '')[:12]}")

    # Health check
    space_url = f"https://skipsnow-{space_name.lower().replace('_', '-')}.hf.space"
    try:
        health = requests.get(f"{space_url}/health", timeout=10)
        if health.status_code == 200:
            print(f"  Health: {health.json()}")
        else:
            print(f"  Health: HTTP {health.status_code}")
    except Exception:
        print("  Health: unreachable")

    # Root check
    try:
        root = requests.get(space_url, timeout=10)
        print(f"  Root: HTTP {root.status_code}")
    except Exception:
        print("  Root: unreachable")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hf_space_status.py <space_name>")
        sys.exit(1)
    check_status(sys.argv[1])
