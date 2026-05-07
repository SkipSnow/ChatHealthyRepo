# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline trigger and monitor.
# Reads bearer token from pipeline.http, invokes FindCarePipeline via Router,
# monitors status until completion, cleans up pipeline.http after run.
#
# Usage:
#   python pipeline_trigger.py --states MS --specialty-metadata
#   python pipeline_trigger.py --states MS DE --copy-to-frontend
#   python pipeline_trigger.py --status <instance_id>

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
HTTP_FILE = os.path.join(REPO_ROOT, "pipeline.http")
ROUTER_URL = "https://devpipelinemanagmentservice-hqa9f5b0b7b4hqgg.eastus2-01.azurewebsites.net/api/Router"


def _get_bearer_token():
    """Read bearer token from pipeline.http file."""
    if not os.path.exists(HTTP_FILE):
        print(f"ERROR: {HTTP_FILE} not found.")
        print(f"Create it with one line: Bearer <your-token>")
        sys.exit(1)
    with open(HTTP_FILE, encoding="utf-8") as f:
        token = f.read().strip()
    # Support both "Bearer xxx" and just "xxx"
    if token.startswith("Bearer "):
        token = token[7:].strip()
    if not token:
        print(f"ERROR: {HTTP_FILE} is empty.")
        sys.exit(1)
    return token


def _cleanup():
    """Delete pipeline.http after run."""
    try:
        if os.path.exists(HTTP_FILE):
            os.remove(HTTP_FILE)
            print(f"Cleaned up {HTTP_FILE}")
    except Exception as e:
        print(f"WARNING: Could not delete {HTTP_FILE}: {e}")


def trigger(payload, token):
    """Call Router to start FindCarePipeline. Returns instance_id and status_url."""
    import requests

    body = {
        "ChatHealthyTask": "FindCarePipeline",
        "payload": payload,
    }

    resp = requests.post(
        ROUTER_URL,
        json=body,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        timeout=30,
    )

    if resp.status_code == 202:
        data = resp.json()
        instance_id = data.get("id", "")
        status_url = data.get("statusQueryGetUri", "")
        print(f"Pipeline started: {instance_id}")
        print(f"Status URL: {status_url[:100]}...")
        return instance_id, status_url, data
    elif resp.status_code == 401:
        print(f"ERROR: Authentication failed. Check your bearer token in {HTTP_FILE}")
        sys.exit(1)
    else:
        print(f"ERROR: Router returned {resp.status_code}: {resp.text[:500]}")
        sys.exit(1)


def monitor(status_url, poll_seconds=15):
    """Poll the status URL until the orchestration completes."""
    import requests

    print(f"\nMonitoring (poll every {poll_seconds}s)...")
    print("=" * 60)

    last_status = ""
    while True:
        try:
            resp = requests.get(status_url, timeout=15)
            data = resp.json()
            runtime = data.get("runtimeStatus", "Unknown")
            custom = data.get("customStatus", "")

            if custom != last_status:
                last_status = custom
                print(f"  [{runtime}] {custom}", flush=True)

            if runtime in ("Completed", "Failed", "Terminated", "Canceled"):
                print(f"\n{'='*60}")
                print(f"FINAL STATUS: {runtime}")
                if runtime == "Completed":
                    output = data.get("output", {})
                    print(f"Output: {json.dumps(output, indent=2)[:1000]}")
                elif runtime == "Failed":
                    print(f"Error: {data.get('output', 'Unknown error')}")
                print(f"{'='*60}")
                return runtime

        except Exception as e:
            print(f"  [poll error] {e}", flush=True)

        time.sleep(poll_seconds)


def check_status(instance_id, token):
    """Check status of a running pipeline by instance ID."""
    import requests

    status_url = (
        f"https://devpipelinemanagmentservice-hqa9f5b0b7b4hqgg.eastus2-01.azurewebsites.net"
        f"/runtime/webhooks/durabletask/instances/{instance_id}"
        f"?taskHub=DevPipelineManagmentService&connection=Storage"
    )
    # Need system key for direct durable task queries — use Router instead
    resp = requests.post(
        ROUTER_URL,
        json={"ChatHealthyTask": "PipelineStatus", "payload": {"instance_id": instance_id}},
        headers={"Authorization": f"Bearer {token}"},
        timeout=15,
    )
    if resp.status_code == 200:
        print(json.dumps(resp.json(), indent=2))
    else:
        print(f"Status check failed: {resp.status_code} {resp.text[:200]}")


def main():
    parser = argparse.ArgumentParser(description="FindCarePipeline trigger and monitor")
    parser.add_argument("--states", nargs="+", help="State codes to load (e.g. MS DE)")
    parser.add_argument("--specialty-metadata", action="store_true", default=True, help="Load SpecialtyMetaData (default: true)")
    parser.add_argument("--no-specialty-metadata", action="store_true", help="Skip SpecialtyMetaData load")
    parser.add_argument("--copy-to-frontend", action="store_true", default=True, help="Copy to frontend (default: true)")
    parser.add_argument("--no-copy-to-frontend", action="store_true", help="Skip CopyToFrontEnd")
    parser.add_argument("--embedding", action="store_true", help="Enable embeddings")
    parser.add_argument(
        "--start-step",
        type=str,
        default=None,
        help=(
            "Canonical step LABEL string to start from (e.g. "
            '"Step 4: Loading provider data"). Same string the orchestrator '
            "displays in customStatus. Omit to start at Step 1."
        ),
    )
    parser.add_argument("--poll", type=int, default=15, help="Poll interval seconds (default 15)")
    parser.add_argument("--no-cleanup", action="store_true", help="Don't delete pipeline.http after run")
    parser.add_argument("--status", type=str, help="Check status of instance ID (don't trigger new)")
    args = parser.parse_args()

    token = _get_bearer_token()

    if args.status:
        check_status(args.status, token)
        return

    payload = {
        "states": args.states or [],
        "specialty_metadata": not args.no_specialty_metadata,
        "copy_to_frontend": not args.no_copy_to_frontend,
        "embedding_enabled": args.embedding,
    }
    if args.start_step is not None:
        payload["start_step"] = args.start_step

    print(f"FindCarePipeline trigger")
    print(f"  States: {payload['states']}")
    print(f"  Specialty metadata: {payload['specialty_metadata']}")
    print(f"  Copy to frontend: {payload['copy_to_frontend']}")
    print(f"  Embeddings: {payload['embedding_enabled']}")
    print(f"  Start step: {payload.get('start_step', '(default — first step)')}")
    print()

    instance_id, status_url, trigger_data = trigger(payload, token)

    # Save status URL to .http file for human to monitor
    with open(os.path.join(REPO_ROOT, "pipeline_status.http"), "w") as f:
        f.write(f"GET {status_url}\n")
    print(f"Status URL saved to pipeline_status.http")

    result = monitor(status_url, args.poll)

    if not args.no_cleanup:
        _cleanup()
        # Also clean up status file
        try:
            os.remove(os.path.join(REPO_ROOT, "pipeline_status.http"))
        except Exception:
            pass

    sys.exit(0 if result == "Completed" else 1)


if __name__ == "__main__":
    main()
