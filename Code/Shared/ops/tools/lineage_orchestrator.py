# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# AUDIT-CODEHIST-001: Deterministic Python Orchestrator
#
# Pure Python. No AI. Loops forever. Reads control.json for commands.
# Dispatches disposable AI workers via claude CLI. Tracks tokens and duration.
# Human stops it by setting command to "stop" in control.json.
#
# Usage:
#   python Code/Shared/ops/tools/lineage_orchestrator.py
#
# Control:
#   Edit _oneshots/test_output/lineage/control.json
#     "command": "continue" — keep working (default)
#     "command": "stop" — exit after current job
#     "command": "report" — write status, then continue

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
LINEAGE_DIR = REPO_ROOT / "_oneshots/test_output" / "lineage"
CONTROL_FILE = LINEAGE_DIR / "control.json"
PROMPTS_DIR = LINEAGE_DIR / "prompts"

JOBS = [
    {"job": 0, "name": "brain_intent", "checkpoint": "00_brain_intent.json", "prompt": "prompt_job0_brain_intent.md"},
    {"job": 1, "name": "gui_history", "checkpoint": "01_gui_history.json", "prompt": "prompt_job1_gui_history.md"},
    {"job": 2, "name": "prompt_history", "checkpoint": "02_prompt_history.json", "prompt": "prompt_job2_prompt_history.md"},
    {"job": 3, "name": "cross_reference", "checkpoint": "03_cross_reference.json", "prompt": "prompt_job3_cross_reference.md"},
    {"job": 4, "name": "final_report", "checkpoint": "04_final_report.json", "prompt": "prompt_job4_final_report.md"},
]


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def read_control():
    """Read control.json. Create with defaults if missing."""
    if not CONTROL_FILE.exists():
        control = {
            "command": "continue",
            "jobs": [
                {"job": j["job"], "name": j["name"], "status": "pending", "tokens_used": 0, "duration_ms": 0}
                for j in JOBS
            ],
            "total_tokens": 0,
            "started_at": now_iso(),
            "last_heartbeat": now_iso(),
        }
        write_control(control)
        return control
    with open(CONTROL_FILE, encoding="utf-8") as f:
        return json.load(f)


def write_control(control):
    """Write control.json atomically."""
    control["last_heartbeat"] = now_iso()
    tmp = CONTROL_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(control, f, indent=2, ensure_ascii=False)
    tmp.replace(CONTROL_FILE)


def write_status_report(control):
    """Write a human-readable status report."""
    report_path = LINEAGE_DIR / "status_report.txt"
    lines = [
        f"Lineage Audit Status Report — {now_iso()}",
        f"{'=' * 60}",
        f"Command: {control['command']}",
        f"Total tokens: {control['total_tokens']}",
        f"Started: {control.get('started_at', '?')}",
        f"Last heartbeat: {control.get('last_heartbeat', '?')}",
        "",
    ]
    for job in control["jobs"]:
        checkpoint = LINEAGE_DIR / JOBS[job["job"]]["checkpoint"]
        on_disk = checkpoint.exists()
        lines.append(f"  Job {job['job']} ({job['name']}): status={job['status']}, "
                      f"checkpoint={'EXISTS' if on_disk else 'MISSING'}, "
                      f"tokens={job['tokens_used']}, duration={job['duration_ms']}ms")
    lines.append("")
    lines.append("To stop: edit control.json and set command to 'stop'")
    report_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[orchestrator] Status report written to {report_path}")


def spawn_worker(job_def):
    """Spawn a Claude agent to execute one job. Returns (tokens, duration_ms)."""
    prompt_file = PROMPTS_DIR / job_def["prompt"]
    if not prompt_file.exists():
        print(f"[orchestrator] ERROR: Prompt file missing: {prompt_file}")
        return 0, 0

    prompt_text = prompt_file.read_text(encoding="utf-8")

    print(f"[orchestrator] Spawning worker for job {job_def['job']} ({job_def['name']})...")
    start = time.time()

    try:
        result = subprocess.run(
            ["claude", "-p", prompt_text, "--allowedTools", "Read,Grep,Glob,Bash"],
            capture_output=True,
            text=True,
            timeout=900,  # 15 minute max per job
            cwd=str(REPO_ROOT),
        )
        duration_ms = int((time.time() - start) * 1000)

        # Parse token usage from output if available
        tokens = 0
        output = result.stdout + result.stderr
        for line in output.split("\n"):
            if "total_tokens" in line:
                try:
                    tokens = int(line.split("total_tokens")[1].strip(": ,}"))
                except (ValueError, IndexError):
                    pass

        if result.returncode != 0:
            print(f"[orchestrator] Worker exited with code {result.returncode}")
            print(f"[orchestrator] stderr: {result.stderr[:500]}")

        # Check if checkpoint was written
        checkpoint = LINEAGE_DIR / job_def["checkpoint"]
        if checkpoint.exists():
            print(f"[orchestrator] Checkpoint written: {checkpoint.name}")
        else:
            print(f"[orchestrator] WARNING: No checkpoint file after worker completed")

        return tokens, duration_ms

    except subprocess.TimeoutExpired:
        duration_ms = int((time.time() - start) * 1000)
        print(f"[orchestrator] Worker timed out after {duration_ms}ms")
        return 0, duration_ms
    except FileNotFoundError:
        print("[orchestrator] ERROR: 'claude' CLI not found. Is it in PATH?")
        return 0, 0


def main():
    """Main orchestrator loop. Runs forever until stopped."""
    LINEAGE_DIR.mkdir(parents=True, exist_ok=True)
    PROMPTS_DIR.mkdir(parents=True, exist_ok=True)

    print(f"[orchestrator] Starting at {now_iso()}")
    print(f"[orchestrator] Control file: {CONTROL_FILE}")
    print(f"[orchestrator] To stop: edit control.json and set command to 'stop'")
    print()

    while True:
        # 1. Read control file
        control = read_control()

        # 2. Check command
        if control["command"] == "stop":
            print(f"[orchestrator] Stop command received. Exiting.")
            write_status_report(control)
            break

        if control["command"] == "report":
            write_status_report(control)
            control["command"] = "continue"
            write_control(control)
            time.sleep(5)
            continue

        # 3. Find next job to run
        next_job = None
        next_job_def = None
        for job_entry in control["jobs"]:
            job_def = JOBS[job_entry["job"]]
            checkpoint = LINEAGE_DIR / job_def["checkpoint"]

            # If checkpoint exists on disk, mark completed regardless of status
            if checkpoint.exists() and job_entry["status"] != "completed":
                job_entry["status"] = "completed"
                print(f"[orchestrator] Job {job_entry['job']} ({job_entry['name']}): "
                      f"checkpoint found on disk, marking completed")
                write_control(control)
                continue

            if job_entry["status"] == "pending":
                next_job = job_entry
                next_job_def = job_def
                break

        # 4. All done? Keep polling — never exit
        if next_job is None:
            # All jobs complete, but keep running
            all_complete = all(j["status"] == "completed" for j in control["jobs"])
            if all_complete:
                print(f"[orchestrator] All jobs complete. Polling for new commands...")
                write_status_report(control)
            time.sleep(30)
            continue

        # 5. Dispatch worker
        next_job["status"] = "in_progress"
        write_control(control)

        tokens, duration_ms = spawn_worker(next_job_def)

        # 6. Record results
        checkpoint = LINEAGE_DIR / next_job_def["checkpoint"]
        if checkpoint.exists():
            next_job["status"] = "completed"
        else:
            next_job["status"] = "pending"  # Will retry on next loop
            print(f"[orchestrator] Job {next_job['job']} failed — will retry")

        next_job["tokens_used"] = tokens
        next_job["duration_ms"] = duration_ms
        control["total_tokens"] = sum(j["tokens_used"] for j in control["jobs"])
        write_control(control)

        # 7. Brief pause before next job
        time.sleep(5)


if __name__ == "__main__":
    main()
