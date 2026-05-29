# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline trigger and monitor.
#
# Per EPIC-010-F-102-S-007:
#   REQ-B-001 — the only entrance for invoking any pipeline is the HTTP
#     POST 'Router' route on the Function App DevPipelineManagementService.
#   REQ-B-002 — the pipeline name (ChatHealthyTask JSON body field) is the
#     request's first positional argument and has no default. The payload
#     object carries the rest; its shape varies by pipeline.
#
# Reads bearer token from pipeline.http, POSTs once to Router with
# {ChatHealthyTask: <pipeline_name>, payload: <payload>}, monitors status
# to completion, cleans up pipeline.http after the run.
#
# Usage:
#   python pipeline_trigger.py ProviderPipeline \
#       --payload-json '{"states":["CA"],"pool_size":100,"batch_size":3000}'
#   python pipeline_trigger.py SpecialtyPipeline --payload-json '{}'
#   python pipeline_trigger.py --status <instance_id>

import argparse
import json
import os
import sys
import time

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
HTTP_FILE = os.path.join(REPO_ROOT, "pipeline.http")
ROUTER_URL = (
    "https://dev-pipeline-records-as-messages.yellowbay-d50afa88."
    "eastus2.azurecontainerapps.io/api/Router"
)


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


def trigger(pipeline_name, payload, token):
    """POST Router with {ChatHealthyTask: <pipeline_name>, payload}.

    Returns (instance_id, status_url, raw_response_dict).
    """
    import requests

    body = {
        "ChatHealthyTask": pipeline_name,
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
    parser = argparse.ArgumentParser(
        description="Trigger a pipeline via the Function App Router and monitor it.",
    )
    parser.add_argument(
        "pipeline_name",
        nargs="?",
        help=(
            "Pipeline name (the ChatHealthyTask body field). Required unless "
            "--status. Per EPIC-010-F-102-S-007-REQ-B-002: first positional, "
            "no default."
        ),
    )
    parser.add_argument(
        "--payload-json",
        type=str,
        default="{}",
        help=(
            "JSON object string for the payload (default '{}'). Shape varies "
            "by pipeline; see the orchestrator for required fields."
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

    if not args.pipeline_name:
        parser.error("pipeline_name is required (or use --status <instance_id>)")

    try:
        payload = json.loads(args.payload_json)
    except json.JSONDecodeError as e:
        parser.error(f"--payload-json is not valid JSON: {e}")
    if not isinstance(payload, dict):
        parser.error("--payload-json must be a JSON object")

    print(f"{args.pipeline_name} trigger")
    print(f"  payload: {json.dumps(payload)}")
    print()

    instance_id, status_url, trigger_data = trigger(args.pipeline_name, payload, token)

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
