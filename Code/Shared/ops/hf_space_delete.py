# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Delete a HuggingFace Space.
# Usage:
#   python hf_space_delete.py --env dev
#   python hf_space_delete.py --env qa
#   python hf_space_delete.py --env prod  (requires --confirm-prod flag)
#
# Safety: prod deletion requires explicit --confirm-prod flag.

import argparse
import logging
import os
import sys

import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("hf_space_delete")

ORG = "SkipSnow"
BASE_NAME = "ChatHealthySpace"


def space_name(env: str) -> str:
    if env == "prod":
        return BASE_NAME
    return f"{env}_{BASE_NAME}"


def delete_space(env: str, confirm_prod: bool = False, dry_run: bool = False):
    token = os.getenv("HF_TOKEN")
    if not token:
        log.error("HF_TOKEN not set in .env")
        sys.exit(1)

    if env == "prod" and not confirm_prod:
        log.error("REFUSED: deleting prod Space requires --confirm-prod flag")
        sys.exit(1)

    name = space_name(env)
    full_name = f"{ORG}/{name}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    log.info("Deleting Space: %s (env=%s)", full_name, env)

    if dry_run:
        log.info("DRY RUN — no changes made")
        return

    resp = requests.delete(
        "https://huggingface.co/api/repos/delete",
        headers=headers,
        json={"type": "space", "name": full_name},
        timeout=15,
    )

    if resp.status_code == 200:
        log.info("DELETED: %s", full_name)
    elif resp.status_code == 404:
        log.info("Space does not exist: %s", full_name)
    else:
        log.error("Failed: %s %s", resp.status_code, resp.text[:200])
        sys.exit(1)


def delete_space_by_name(name: str, dry_run: bool = False):
    """Delete a Space by explicit name (for non-standard/legacy spaces)."""
    token = os.getenv("HF_TOKEN")
    if not token:
        log.error("HF_TOKEN not set in .env")
        sys.exit(1)

    full_name = f"{ORG}/{name}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    log.info("Deleting Space: %s", full_name)

    if dry_run:
        log.info("DRY RUN — no changes made")
        return

    resp = requests.delete(
        "https://huggingface.co/api/repos/delete",
        headers=headers,
        json={"type": "space", "name": full_name},
        timeout=15,
    )

    if resp.status_code == 200:
        log.info("DELETED: %s", full_name)
    elif resp.status_code == 404:
        log.info("Space does not exist: %s", full_name)
    else:
        log.error("Failed: %s %s", resp.status_code, resp.text[:200])
        sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete a ChatHealthy HuggingFace Space")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--env", choices=["dev", "qa", "prod"],
                       help="Environment: dev, qa, or prod (uses naming convention)")
    group.add_argument("--name",
                       help="Explicit Space name (for legacy/non-standard spaces)")
    parser.add_argument("--confirm-prod", action="store_true",
                        help="Required to delete prod Space")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print plan without making changes")
    args = parser.parse_args()

    if args.name:
        delete_space_by_name(args.name, args.dry_run)
    else:
        delete_space(args.env, args.confirm_prod, args.dry_run)
