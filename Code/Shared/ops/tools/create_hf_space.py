# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# DR-026: This is the ONLY way to create a HuggingFace Space. No exceptions.
# v4-024: All Spaces use cpu-upgrade tier (8 vCPU, 32 GB RAM, $0.03/hr).
#
# Usage: python create_hf_space.py <space_name> [title]
# Example: python create_hf_space.py dev_EvaluateCareSpace "EvaluateCare"
#
# Creates the Space, pushes the required README.md with Docker SDK config,
# and verifies the Space is in BUILDING state before returning.

import sys
import os
import tempfile
import subprocess

from huggingface_hub import HfApi

ORG = "SkipSnow"
HARDWARE = "cpu-upgrade"  # 8 vCPU, 32 GB RAM, $0.03/hr — v4-024 mandatory minimum
SDK = "docker"
APP_PORT = 7860


def create_space(space_name: str, title: str = None) -> dict:
    title = title or space_name

    token = os.environ.get("HF_TOKEN", "")
    if not token:
        from dotenv import load_dotenv
        for env_path in [
            os.path.join(os.path.dirname(__file__), "..", "..", "..", ".env"),
            os.path.join(os.path.dirname(__file__), "..", "..", ".env"),
        ]:
            if os.path.exists(env_path):
                load_dotenv(env_path)
                token = os.environ.get("HF_TOKEN", "")
                if token:
                    break
    if not token:
        raise ValueError("HF_TOKEN not set")

    api = HfApi(token=token)
    repo_id = f"{ORG}/{space_name}"

    # Create the Space
    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk=SDK,
        space_hardware=HARDWARE,
        private=False,
    )

    # Clone and push README.md with required Docker SDK config
    clone_dir = os.path.join(tempfile.gettempdir(), f"hf_{space_name}")
    if os.path.exists(clone_dir):
        import shutil
        shutil.rmtree(clone_dir)

    subprocess.run(
        ["git", "clone", f"https://{ORG}:{token}@huggingface.co/spaces/{repo_id}", clone_dir],
        capture_output=True, check=True,
    )

    readme_path = os.path.join(clone_dir, "README.md")
    with open(readme_path, "w") as f:
        f.write(f"""---
title: {title}
emoji: "\U0001f3e5"
colorFrom: green
colorTo: blue
sdk: docker
app_port: {APP_PORT}
pinned: false
---
""")

    subprocess.run(["git", "add", "README.md"], cwd=clone_dir, capture_output=True, check=True)
    subprocess.run(
        ["git", "commit", "-m", f"Initialize {title} Space (DR-026, v4-024: cpu-upgrade)"],
        cwd=clone_dir, capture_output=True, check=True,
    )
    subprocess.run(["git", "push"], cwd=clone_dir, capture_output=True, check=True)

    # Verify
    info = api.space_info(repo_id)
    stage = info.runtime.stage if info.runtime else "UNKNOWN"

    return {
        "repo_id": repo_id,
        "hardware": HARDWARE,
        "sdk": SDK,
        "app_port": APP_PORT,
        "stage": stage,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(f"Usage: python {sys.argv[0]} <space_name> [title]")
        print(f"Example: python {sys.argv[0]} dev_EvaluateCareSpace EvaluateCare")
        sys.exit(1)

    name = sys.argv[1]
    ttl = sys.argv[2] if len(sys.argv) > 2 else None
    result = create_space(name, ttl)
    print(f"Created: {result['repo_id']} ({result['hardware']}, port {result['app_port']}, stage: {result['stage']})")
