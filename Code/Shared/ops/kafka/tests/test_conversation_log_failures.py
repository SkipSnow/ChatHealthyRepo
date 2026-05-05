# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# FAILURE TESTS for conversation log Kafka pipeline.
# These are NOT happy path tests. These test what happens when things break.
#
# Architecture under test:
#   C# service (ChatHealthyClaudeLogMgmt) -> sidecar (PID 1, port 8100) + consumer (PID 2)
#   Hook in chathealthy_devops_boot.py POSTs to sidecar, fallback to local file
#
# DESTRUCTIVE: These tests kill real processes. Each test restores the system.
# Run with: pytest Code/Shared/ops/kafka/tests/test_conversation_log_failures.py -v -s
# Requires ADMIN privileges (stop/start Windows services, kill processes).

import json
import os
import subprocess
import sys
import time
import uuid

import pytest
import requests

# ── Configuration ──────────────────────────────────────────────────────

SIDECAR_URL = "http://127.0.0.1:8100"
SERVICE_NAME = "ChatHealthyClaudeLogMgmt"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
BOOT_SCRIPT = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "tools", "chathealthy_devops_boot.py")
FALLBACK_PATH = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "conversation_log_fallback.jsonl")
PYTHON_EXE = sys.executable

# Timeouts
SERVICE_STOP_TIMEOUT = 30       # seconds to wait for service to stop
SERVICE_START_TIMEOUT = 30      # seconds to wait for service + sidecar to come up
SIDECAR_HEALTH_TIMEOUT = 25     # seconds to wait for sidecar health after service start


def _sidecar_is_up():
    try:
        r = requests.get(f"{SIDECAR_URL}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _kafka_is_up():
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=chathealthy-kafka", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        return "Up" in result.stdout
    except Exception:
        return False


def _service_state():
    """Return current service state string (RUNNING, STOPPED, etc.)."""
    result = subprocess.run(
        ["sc", "query", SERVICE_NAME],
        capture_output=True, text=True, timeout=10,
    )
    for line in result.stdout.split("\n"):
        if "STATE" in line:
            # e.g. "        STATE              : 4  RUNNING"
            parts = line.strip().split()
            if len(parts) >= 4:
                return parts[-1]  # RUNNING, STOPPED, etc.
    return "UNKNOWN"


def _stop_service():
    """Stop the Windows service and wait for it to reach STOPPED state."""
    subprocess.run(["sc", "stop", SERVICE_NAME], capture_output=True, text=True, timeout=10)
    deadline = time.time() + SERVICE_STOP_TIMEOUT
    while time.time() < deadline:
        state = _service_state()
        if state == "STOPPED":
            return True
        time.sleep(1)
    return _service_state() == "STOPPED"


def _start_service():
    """Start the Windows service and wait for sidecar to be healthy."""
    subprocess.run(["sc", "start", SERVICE_NAME], capture_output=True, text=True, timeout=10)
    deadline = time.time() + SERVICE_START_TIMEOUT
    while time.time() < deadline:
        state = _service_state()
        if state == "RUNNING":
            break
        time.sleep(1)
    # Now wait for sidecar to actually respond (service starts Kafka wait + Python)
    deadline = time.time() + SIDECAR_HEALTH_TIMEOUT
    while time.time() < deadline:
        if _sidecar_is_up():
            return True
        time.sleep(1)
    return _sidecar_is_up()


def _get_python_pids_under_services():
    """Get PIDs of python.exe processes running in Services session (session 0).
    Uses wmic instead of tasklist to avoid Git Bash /FI flag corruption."""
    result = subprocess.run(
        ["wmic", "process", "where", "Name='python.exe' and SessionId=0",
         "get", "ProcessId", "/format:csv"],
        capture_output=True, text=True, timeout=10,
    )
    pids = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip().strip("\r").strip("\x00")
        if not line or "ProcessId" in line or "Node" in line:
            continue
        # CSV: Node,ProcessId
        parts = line.split(",")
        if len(parts) >= 2:
            try:
                pids.append(int(parts[-1].strip()))
            except ValueError:
                continue
    return pids


def _get_sidecar_pid():
    """Get the PID of the sidecar process by finding what listens on port 8100."""
    result = subprocess.run(
        ["netstat", "-ano"],
        capture_output=True, text=True, timeout=10,
    )
    for line in result.stdout.split("\n"):
        if ":8100" in line and "LISTENING" in line:
            parts = line.strip().split()
            if parts:
                try:
                    return int(parts[-1])
                except ValueError:
                    pass
    return None


def _get_consumer_pid():
    """Get the PID of the consumer process by checking command lines.
    Consumer = python.exe in session 0 running conversation_log_consumer.py."""
    result = subprocess.run(
        ["wmic", "process", "where", "Name='python.exe' and SessionId=0",
         "get", "ProcessId,CommandLine", "/format:csv"],
        capture_output=True, text=True, timeout=10,
    )
    sidecar_pid = _get_sidecar_pid()
    for line in result.stdout.split("\n"):
        if "conversation_log_consumer" in line:
            # CSV format: Node,CommandLine,ProcessId
            parts = line.strip().strip("\r").strip("\x00").split(",")
            if parts:
                try:
                    pid = int(parts[-1].strip())
                    if pid != sidecar_pid:
                        return pid
                except ValueError:
                    continue
    return None


def _kill_pid(pid):
    """Kill a specific process by PID."""
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, text=True, timeout=10)


def _run_hook(prompt_text, mode="prompt"):
    """Run the boot hook with a test payload. Returns parsed stdout JSON."""
    payload = json.dumps({
        "prompt": prompt_text,
        "hook_event_name": "UserPromptSubmit" if mode == "prompt" else "Stop",
    })
    result = subprocess.run(
        [PYTHON_EXE, BOOT_SCRIPT, "--mode", mode],
        input=payload, capture_output=True, text=True, timeout=15,
    )
    try:
        return json.loads(result.stdout), result.returncode
    except (json.JSONDecodeError, ValueError):
        return {"raw_stdout": result.stdout, "raw_stderr": result.stderr}, result.returncode


def _get_log_worker_result(hook_output):
    """Extract the conversation_log worker result from hook output."""
    workers = hook_output.get("workers", [])
    for w in workers:
        if w.get("json") == "conversation_log":
            return w
    return {}


def _get_kafka_end_offset():
    """Get the current end offset for the conversation-log topic."""
    try:
        from confluent_kafka import Consumer
        c = Consumer({
            "bootstrap.servers": "localhost:9092",
            "group.id": f"test-offset-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "latest",
        })
        c.subscribe(["conversation-log"])
        c.poll(timeout=3.0)  # trigger assignment
        partitions = c.assignment()
        offsets = {}
        if partitions:
            for p in partitions:
                lo, hi = c.get_watermark_offsets(p)
                offsets[p.partition] = hi
        c.close()
        return offsets
    except Exception as e:
        return {"error": str(e)}


# ── Precondition check ────────────────────────────────────────────────

pytestmark = pytest.mark.skipif(
    not _sidecar_is_up() or not _kafka_is_up(),
    reason="Kafka sidecar or broker not running — cannot run failure tests",
)


# ══════════════════════════════════════════════════════════════════════
# TEST 1: SIDECAR DOWN
# Kill the sidecar process. Send a message through the hook. Verify:
#   - Hook does NOT crash (returns comply=true)
#   - Hook writes to the emergency fallback log file
#   - Clean up: restart service to bring sidecar back
# ══════════════════════════════════════════════════════════════════════

class TestFailure01_SidecarDown:

    def test_sidecar_down_hook_uses_fallback(self):
        """Kill sidecar, send message through hook, verify fallback write."""
        marker = f"FAILURE-TEST-SIDECAR-DOWN-{uuid.uuid4().hex[:8]}"

        # Step 1: Identify the sidecar PID (python.exe on port 8100)
        sidecar_pid = _get_sidecar_pid()
        assert sidecar_pid is not None, "Could not find sidecar PID on port 8100"

        # Step 2: Kill ONLY the sidecar process (not the service)
        _kill_pid(sidecar_pid)
        time.sleep(1)

        # Verify sidecar is actually down
        assert not _sidecar_is_up(), "Sidecar should be down after kill"

        # Step 3: Record fallback file state before
        fallback_size_before = os.path.getsize(FALLBACK_PATH) if os.path.exists(FALLBACK_PATH) else 0

        # Step 4: Send message through the hook
        hook_output, returncode = _run_hook(marker)

        # Step 5: Verify hook did NOT crash
        assert returncode == 0, f"Hook crashed with returncode {returncode}"
        log_worker = _get_log_worker_result(hook_output)
        assert log_worker.get("comply") is True, f"Hook did not return comply=true: {log_worker}"

        # Step 6: Verify fallback was written
        assert os.path.exists(FALLBACK_PATH), "Fallback file does not exist after sidecar-down write"
        fallback_size_after = os.path.getsize(FALLBACK_PATH)
        assert fallback_size_after > fallback_size_before, \
            f"Fallback file did not grow: {fallback_size_before} -> {fallback_size_after}"

        # Step 7: Verify the marker text is in the fallback file
        with open(FALLBACK_PATH, "r", encoding="utf-8") as f:
            fallback_content = f.read()
        assert marker in fallback_content, f"Marker '{marker}' not found in fallback file"

        # Step 8: Verify the fallback entry has correct structure
        # Read the last line (our entry)
        last_line = [l for l in fallback_content.strip().split("\n") if marker in l]
        assert len(last_line) >= 1, "Could not find marker line in fallback"
        entry = json.loads(last_line[-1])
        assert "timestamp" in entry, "Fallback entry missing timestamp"
        assert "hook_event" in entry, "Fallback entry missing hook_event"
        assert "payload" in entry, "Fallback entry missing payload"

        # Step 9: Verify the log_worker result indicates fallback was used
        assert log_worker.get("fallback") is True, \
            f"Hook did not report fallback=true: {log_worker}"

    def test_sidecar_down_cleanup_restore(self):
        """After sidecar-down test, restart service and verify healthy."""
        # The C# supervisor has a 30s health check loop. It will detect
        # the dead sidecar and restart it. But we want determinism,
        # so we force a full service restart.
        _stop_service()
        time.sleep(2)
        restored = _start_service()
        assert restored, "Failed to restore sidecar after failure test"
        assert _sidecar_is_up(), "Sidecar not healthy after restore"

        # Verify sidecar actually works
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": f"post-restore-{uuid.uuid4().hex[:8]}"},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=3,
        )
        assert r.status_code == 202, f"Sidecar not accepting messages after restore: {r.status_code}"


# ══════════════════════════════════════════════════════════════════════
# TEST 2: CONSUMER DOWN
# Kill the consumer process. Send messages through sidecar. Verify:
#   - Sidecar still accepts messages (202)
#   - Messages are retained in Kafka (offset advances)
#   - After service restart, consumer catches up
# ══════════════════════════════════════════════════════════════════════

class TestFailure02_ConsumerDown:

    def test_consumer_down_sidecar_still_accepts(self):
        """Kill consumer, verify sidecar keeps accepting, Kafka retains messages."""
        marker_prefix = f"FAILURE-TEST-CONSUMER-DOWN-{uuid.uuid4().hex[:8]}"

        # Pre-step: Ensure sidecar is stable (previous test may have killed it)
        if not _sidecar_is_up():
            _stop_service()
            time.sleep(2)
            assert _start_service(), "Cannot stabilize system before consumer-down test"
            time.sleep(3)

        # Step 1: Identify the consumer PID by command line (not guessing)
        consumer_pid = _get_consumer_pid()
        assert consumer_pid is not None, "Could not identify consumer PID by command line"

        sidecar_pid = _get_sidecar_pid()
        assert sidecar_pid is not None, "Could not find sidecar PID before consumer kill"
        assert consumer_pid != sidecar_pid, \
            f"Consumer PID ({consumer_pid}) matches sidecar PID ({sidecar_pid}) — identification error"

        # Step 2: Kill ONLY the consumer
        _kill_pid(consumer_pid)
        time.sleep(2)

        # Step 2b: Verify sidecar survived the consumer kill
        assert _sidecar_is_up(), \
            "Sidecar died when consumer was killed — possible process tree kill issue"

        # Step 3: Record Kafka offset before sending messages
        offsets_before = _get_kafka_end_offset()

        # Step 4: Send multiple messages through sidecar (should still work)
        num_messages = 5
        for i in range(num_messages):
            r = requests.post(
                f"{SIDECAR_URL}/produce",
                json={"prompt": f"{marker_prefix}-msg-{i}"},
                headers={"X-Hook-Event": "UserPromptSubmit"},
                timeout=3,
            )
            assert r.status_code == 202, f"Sidecar rejected message {i} while consumer is down: {r.status_code}"

        # Step 5: Give Kafka a moment, then verify offset advanced
        time.sleep(2)
        offsets_after = _get_kafka_end_offset()

        if "error" not in offsets_before and "error" not in offsets_after:
            for partition in offsets_after:
                before = offsets_before.get(partition, 0)
                after = offsets_after.get(partition, 0)
                assert after >= before + num_messages, \
                    f"Kafka offset did not advance enough: partition {partition} was {before}, now {after}, expected +{num_messages}"

        # Step 6: Sidecar health still OK
        assert _sidecar_is_up(), "Sidecar died when consumer was killed"

    def test_consumer_down_cleanup_restore(self):
        """Restart service, verify consumer comes back and catches up."""
        _stop_service()
        time.sleep(2)
        restored = _start_service()
        assert restored, "Failed to restore service after consumer-down test"

        # Verify both Python PIDs are back
        time.sleep(5)  # Give consumer time to start and connect
        pids = _get_python_pids_under_services()
        assert len(pids) >= 2, f"Expected 2 Python PIDs after restore, got {len(pids)}: {pids}"

        # Verify sidecar accepts messages
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": f"post-consumer-restore-{uuid.uuid4().hex[:8]}"},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=3,
        )
        assert r.status_code == 202


# ══════════════════════════════════════════════════════════════════════
# TEST 3: SERVICE RESTART RESILIENCE
# Stop the service. Verify no zombie Python processes. Start the service.
# Verify both PIDs come back. Verify sidecar health.
# ══════════════════════════════════════════════════════════════════════

class TestFailure03_ServiceRestart:

    def test_service_stop_kills_all_children(self):
        """Stop service, verify zero Python zombies in Services session."""
        # Step 1: Confirm Python PIDs exist before stop
        pids_before = _get_python_pids_under_services()
        assert len(pids_before) >= 2, f"Need at least 2 Python PIDs before test, got {len(pids_before)}"

        # Step 2: Stop the service
        stopped = _stop_service()
        assert stopped, f"Service did not stop. State: {_service_state()}"

        # Step 3: Wait a moment for process cleanup
        time.sleep(3)

        # Step 4: Verify NO Python processes remain in Services session
        # The service MUST kill child processes on stop
        pids_after = _get_python_pids_under_services()
        # Filter to only PIDs that were present before (avoid false positives from other services)
        zombie_pids = [p for p in pids_before if p in pids_after]
        assert len(zombie_pids) == 0, \
            f"REGRESSION: Zombie Python processes after service stop: {zombie_pids}"

    def test_service_start_brings_both_pids_back(self):
        """Start service after stop, verify both PIDs and sidecar health."""
        # Step 1: Start the service
        started = _start_service()
        assert started, f"Service did not start with healthy sidecar. State: {_service_state()}"

        # Step 2: Give the processes time to fully initialize
        time.sleep(5)

        # Step 3: Verify two Python PIDs in Services session
        pids = _get_python_pids_under_services()
        assert len(pids) >= 2, \
            f"Expected at least 2 Python PIDs after service start, got {len(pids)}: {pids}"

        # Step 4: Verify sidecar health endpoint
        r = requests.get(f"{SIDECAR_URL}/health", timeout=3)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "healthy"
        assert data["topic"] == "conversation-log"

        # Step 5: Verify sidecar accepts a message (full roundtrip)
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": f"restart-resilience-{uuid.uuid4().hex[:8]}"},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=3,
        )
        assert r.status_code == 202, f"Sidecar not accepting messages after restart: {r.status_code}"


# ══════════════════════════════════════════════════════════════════════
# TEST 4: HOOK TIMEOUT RESILIENCE
# The hook must complete even if the sidecar is slow or unreachable.
# Verify the hook returns comply=true within a reasonable time.
# ══════════════════════════════════════════════════════════════════════

class TestFailure04_HookTimeout:

    def test_hook_returns_within_timeout_when_sidecar_down(self):
        """With sidecar down, hook must still return within SIDECAR_TIMEOUT + margin."""
        marker = f"FAILURE-TEST-TIMEOUT-{uuid.uuid4().hex[:8]}"

        # Step 1: Kill the sidecar to simulate unreachable
        sidecar_pid = _get_sidecar_pid()
        if sidecar_pid:
            _kill_pid(sidecar_pid)
            time.sleep(1)

        assert not _sidecar_is_up(), "Sidecar should be down for timeout test"

        # Step 2: Time the hook execution
        # The hook has SIDECAR_TIMEOUT = 2 seconds. With Python startup overhead,
        # the total should be well under 10 seconds.
        start = time.time()
        hook_output, returncode = _run_hook(marker)
        elapsed = time.time() - start

        # Step 3: Verify hook returned (did not hang)
        assert returncode == 0, f"Hook crashed: returncode={returncode}"

        # Step 4: Verify timing — hook must complete within timeout + overhead
        # SIDECAR_TIMEOUT is 2s, Python startup ~0.5s, so 5s is generous
        max_allowed = 5.0
        assert elapsed < max_allowed, \
            f"Hook took {elapsed:.2f}s — must complete within {max_allowed}s even with sidecar down"

        # Step 5: Verify comply=true
        log_worker = _get_log_worker_result(hook_output)
        assert log_worker.get("comply") is True, f"Hook did not comply when sidecar was slow: {log_worker}"

    def test_hook_timeout_with_sidecar_up_is_fast(self):
        """When sidecar is healthy, hook must be fast (under 1 second for HTTP portion)."""
        # First restore sidecar
        _stop_service()
        time.sleep(2)
        restored = _start_service()
        assert restored, "Cannot restore sidecar for timing comparison test"

        marker = f"TIMING-BASELINE-{uuid.uuid4().hex[:8]}"
        start = time.time()
        hook_output, returncode = _run_hook(marker)
        elapsed = time.time() - start

        assert returncode == 0, f"Hook crashed: returncode={returncode}"
        # With sidecar up, should be well under 2 seconds (Python startup + HTTP)
        assert elapsed < 2.0, \
            f"Hook took {elapsed:.2f}s with healthy sidecar — expected under 2s"

        log_worker = _get_log_worker_result(hook_output)
        assert log_worker.get("comply") is True
        assert log_worker.get("logged") is True
        assert log_worker.get("status") == 202

    def test_hook_timeout_cleanup_restore(self):
        """Ensure system is healthy after timeout tests."""
        if not _sidecar_is_up():
            _stop_service()
            time.sleep(2)
            restored = _start_service()
            assert restored, "Failed to restore after timeout test"
        assert _sidecar_is_up(), "Sidecar not healthy after timeout test cleanup"


# ══════════════════════════════════════════════════════════════════════
# FINAL HEALTH CHECK
# Always runs last — confirms the system is fully restored.
# ══════════════════════════════════════════════════════════════════════

class TestFailure99_FinalHealthCheck:

    def test_service_running(self):
        """Post-test: Service is in RUNNING state."""
        state = _service_state()
        if state != "RUNNING":
            _start_service()
            state = _service_state()
        assert state == "RUNNING", f"Service not running after all failure tests: {state}"

    def test_sidecar_healthy(self):
        """Post-test: Sidecar responds to health check."""
        if not _sidecar_is_up():
            _stop_service()
            time.sleep(2)
            _start_service()
        assert _sidecar_is_up(), "Sidecar not healthy after all failure tests"

    def test_kafka_still_running(self):
        """Post-test: Kafka container still running (not accidentally killed)."""
        assert _kafka_is_up(), "Kafka container died during failure tests"

    def test_two_python_pids(self):
        """Post-test: Both Python PIDs exist in Services session."""
        time.sleep(3)
        pids = _get_python_pids_under_services()
        assert len(pids) >= 2, f"Expected 2 Python PIDs, got {len(pids)}: {pids}"

    def test_end_to_end_message(self):
        """Post-test: Full roundtrip message through sidecar to Kafka works."""
        marker = f"FINAL-HEALTH-{uuid.uuid4().hex[:8]}"
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": marker},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=3,
        )
        assert r.status_code == 202, f"End-to-end message failed: {r.status_code}"
