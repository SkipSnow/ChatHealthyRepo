# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Restart a HuggingFace Space. Supports normal and factory restart.
# Usage:
#   python hf_space_restart.py <space_name> [--factory]
#   python hf_space_restart.py dev_ChatHealthyAIChatWindow --factory

import os
import sys
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))


def restart_space(space_name: str, factory: bool = False):
    token = os.getenv("HF_TOKEN")
    if not token:
        print("ERROR: HF_TOKEN not set")
        sys.exit(1)

    url = f"https://huggingface.co/api/spaces/SkipSnow/{space_name}/restart"
    if factory:
        url += "?factory=true"

    resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=15)
    if resp.status_code == 200:
        data = resp.json()
        print(f"{'Factory restart' if factory else 'Restart'}: {space_name}")
        print(f"  Stage: {data.get('stage')}")
        print(f"  Hardware: {data.get('hardware')}")
    else:
        print(f"ERROR: {resp.status_code} {resp.text[:200]}")
        sys.exit(1)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python hf_space_restart.py <space_name> [--factory]")
        sys.exit(1)
    space = sys.argv[1]
    factory = "--factory" in sys.argv
    restart_space(space, factory)
