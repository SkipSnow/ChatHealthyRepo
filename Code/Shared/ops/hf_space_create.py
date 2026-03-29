# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Create a HuggingFace Space for ChatHealthy with environment-specific configuration.
# Usage:
#   python hf_space_create.py --env dev --hardware cpu-upgrade
#   python hf_space_create.py --env qa --hardware cpu-upgrade
#   python hf_space_create.py --env prod --hardware cpu-upgrade
#
# Naming convention:
#   dev  → dev_ChatHealthySpace
#   qa   → qa_ChatHealthySpace
#   prod → ChatHealthySpace

import argparse
import json
import logging
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hf_space_create")

ORG = "SkipSnow"
BASE_NAME = "ChatHealthySpace"

# Hardware tiers — validated against HF API
VALID_HARDWARE = {
    "cpu-basic",       # Free
    "cpu-upgrade",     # $0.03/hr — minimum paid tier
    "t4-small",        # $0.60/hr
    "t4-medium",       # $0.90/hr
    "a10g-small",      # $1.05/hr
    "a10g-large",      # $3.15/hr
}

# Environment-specific secrets — read from local .env
# These are the secrets every ChatHealthy Space needs.
SECRET_KEYS = [
    "MONGO_FRONTEND_connectionString",
    "Anthropic_API_KEY",
    "OPENAI_API_KEY",
    "SPARKMAIL_API_KEY",
    "NOTIFICATION_FROM_EMAIL",
    "NOTIFICATION_TO_EMAIL",
    "ADMIN_UNLOCK_KEY",
    "GOOGLE_MAPS_API_KEY",
]


def space_name(env: str) -> str:
    """Derive Space name from environment."""
    if env == "prod":
        return BASE_NAME
    return f"{env}_{BASE_NAME}"


def create_space(env: str, hardware: str, dry_run: bool = False):
    token = os.getenv("HF_TOKEN")
    if not token:
        log.error("HF_TOKEN not set in .env")
        sys.exit(1)

    if hardware not in VALID_HARDWARE:
        log.error("Invalid hardware '%s'. Valid: %s", hardware, ", ".join(sorted(VALID_HARDWARE)))
        sys.exit(1)

    name = space_name(env)
    full_name = f"{ORG}/{name}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    log.info("Creating Space: %s (env=%s, hardware=%s)", full_name, env, hardware)

    if dry_run:
        log.info("DRY RUN — no changes made")
        _print_plan(env, name, hardware)
        return

    # Step 1: Create the Space
    log.info("Step 1/4: Creating repo...")
    resp = requests.post(
        "https://huggingface.co/api/repos/create",
        headers=headers,
        json={"type": "space", "name": name, "sdk": "docker", "private": False},
        timeout=15,
    )
    if resp.status_code == 200:
        log.info("  Space created: %s", full_name)
    elif resp.status_code == 409:
        log.info("  Space already exists: %s", full_name)
    else:
        log.error("  Failed: %s %s", resp.status_code, resp.text[:200])
        sys.exit(1)

    # Step 2: Set hardware
    log.info("Step 2/4: Setting hardware to %s...", hardware)
    resp = requests.post(
        f"https://huggingface.co/api/spaces/{full_name}/hardware",
        headers=headers,
        json={"flavor": hardware},
        timeout=15,
    )
    if resp.status_code == 200:
        log.info("  Hardware set: %s", hardware)
    else:
        log.warning("  Hardware set failed: %s %s", resp.status_code, resp.text[:200])

    # Step 3: Set secrets
    log.info("Step 3/4: Setting secrets...")
    secrets_set = 0

    # ENV_PREFIX is environment-specific
    _set_secret(headers, full_name, "ENV_PREFIX", env)
    secrets_set += 1

    for key in SECRET_KEYS:
        value = os.getenv(key)
        if value:
            _set_secret(headers, full_name, key, value)
            secrets_set += 1
        else:
            log.warning("  Secret %s not found in .env — skipping", key)

    log.info("  %d secrets set", secrets_set)

    # Step 4: Verify
    log.info("Step 4/4: Verifying...")
    time.sleep(2)
    resp = requests.get(
        f"https://huggingface.co/api/spaces/{full_name}",
        headers=headers,
        timeout=15,
    )
    if resp.status_code == 200:
        data = resp.json()
        runtime = data.get("runtime", {})
        log.info("  Space: %s", full_name)
        log.info("  SDK: %s", data.get("sdk"))
        log.info("  Stage: %s", runtime.get("stage"))
        log.info("  Hardware: %s", runtime.get("hardware"))
    else:
        log.warning("  Verification failed: %s", resp.status_code)

    log.info("DONE. Space %s created. Push code to deploy.", full_name)
    log.info("Deploy workflow must target this Space name.")


def _set_secret(headers, full_name, key, value):
    """Set a secret on a HuggingFace Space."""
    resp = requests.post(
        f"https://huggingface.co/api/spaces/{full_name}/secrets",
        headers=headers,
        json={"key": key, "value": value},
        timeout=15,
    )
    if resp.status_code == 200:
        log.info("  Set: %s", key)
    else:
        log.warning("  Failed to set %s: %s %s", key, resp.status_code, resp.text[:100])


def _print_plan(env, name, hardware):
    """Print what would be created without making changes."""
    full_name = f"{ORG}/{name}"
    print(f"\n  Space: {full_name}")
    print(f"  SDK: docker")
    print(f"  Hardware: {hardware}")
    print(f"  ENV_PREFIX: {env}")
    print(f"  Secrets to set: ENV_PREFIX + {len(SECRET_KEYS)} keys")
    for key in SECRET_KEYS:
        value = os.getenv(key)
        status = "found" if value else "MISSING"
        print(f"    {key}: {status}")
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create a ChatHealthy HuggingFace Space")
    parser.add_argument("--env", required=True, choices=["dev", "qa", "prod"],
                        help="Environment: dev, qa, or prod")
    parser.add_argument("--hardware", default="cpu-upgrade",
                        help=f"Hardware tier (default: cpu-upgrade). Valid: {', '.join(sorted(VALID_HARDWARE))}")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making changes")
    args = parser.parse_args()

    create_space(args.env, args.hardware, args.dry_run)
