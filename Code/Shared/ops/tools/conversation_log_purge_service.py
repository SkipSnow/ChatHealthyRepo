# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthyClaudeLogManagementWindowsService
# Windows service for conversation log archival.
# Implements T001-T002, T012-T020, T023-T024.
#
# Usage:
#   python conversation_log_purge_service.py install
#   python conversation_log_purge_service.py start
#   python conversation_log_purge_service.py stop
#   python conversation_log_purge_service.py remove
#   python conversation_log_purge_service.py --purge-now

import json
import logging
import msvcrt
import os
import re
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

# ── T024: Absolute paths ───────────────────────────────────────────────

REPO_ROOT = Path(r"c:\chatHealthy\findCare")
CONVERSATION_LOG_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"
TEMP_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json.temp"
SCHEMA_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "schema.json"
ENV_PATH = REPO_ROOT / "Code" / ".env"
ERRORS_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "errors.json"
QUEUE_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log_purge_queue.json"
SERVICE_LOG = REPO_ROOT / "test_output" / "conversation_log_service.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(SERVICE_LOG, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
_log = logging.getLogger("ChatHealthyClaudeLogManagementWindowsService")

# Credential patterns for T017 redaction
_CREDENTIAL_PATTERNS = [
    re.compile(r"(sk-ant-api\S+)", re.IGNORECASE),
    re.compile(r"(mongodb\+srv://[^\s\"']+)", re.IGNORECASE),
    re.compile(r"(ANTHROPIC_API_KEY\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(MONGO_FRONTEND_connectionString\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*\S+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*\S+)", re.IGNORECASE),
]


def _load_env():
    """Load Code/.env for mongoConnectionString."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass


def _read_mongo_connection_string():
    """T005: Read mongoConnectionString from Code/.env."""
    _load_env()
    conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
    if not conn:
        _log.error("MONGO_FRONTEND_connectionString not found in environment")
    return conn


def _read_schema():
    """T020: Read schema from the address in conversation_log.json header."""
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _log.error("Failed to read schema: %s", e)
        return None


def _has_old_records(data):
    """T001: Check if conversation_log.json has records older than 24 hours."""
    from conversation_log_agent import _parse_datetime
    pst_offset = timedelta(hours=-7)
    cutoff = datetime.now(timezone.utc) + pst_offset - timedelta(hours=24)
    for u in data.get("utterances", []):
        ts = _parse_datetime(u.get("timestamp_pst", ""))
        if ts and ts < cutoff:
            return True
    return False


def _redact_credentials(data):
    """T017: Redact security tokens from utterance content before sending to agent."""
    for u in data.get("utterances", []):
        content = u.get("content", "")
        if isinstance(content, str):
            for pattern in _CREDENTIAL_PATTERNS:
                content = pattern.sub("[REDACTED]", content)
            u["content"] = content
    return data


def _acquire_lock(file_path, mode="r"):
    """T019: Open file with write lock, retry 5s x 120 (10 minutes)."""
    for attempt in range(120):
        try:
            f = open(file_path, mode, encoding="utf-8")
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            return f
        except (IOError, OSError):
            if attempt < 119:
                time.sleep(5)
            else:
                _log.error("File locked for 10 minutes, giving up: %s", file_path)
                _write_error(f"File locked for 10 minutes: {file_path}")
                return None


def _release_lock(f):
    """T019: Release lock and close file."""
    if f:
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except (IOError, OSError):
            pass
        f.close()


def _write_error(message):
    """T023/T024: Write error to errors.json."""
    try:
        errors = []
        if ERRORS_PATH.exists():
            with open(ERRORS_PATH, "r", encoding="utf-8") as f:
                errors = json.load(f)
        errors.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ChatHealthyClaudeLogManagementWindowsService",
            "error": message,
        })
        with open(ERRORS_PATH, "w", encoding="utf-8") as f:
            json.dump(errors, f, indent=2, ensure_ascii=False)
    except Exception as e:
        _log.error("Failed to write to errors.json: %s", e)


def _format_pst(dt):
    """B003: Format PST timestamp with microsecond precision."""
    if os.name == "nt":
        return dt.strftime("%#m/%#d/%Y %#H:%M:%S.") + f"{dt.microsecond:06d}"
    return dt.strftime("%-m/%-d/%Y %-H:%M:%S.%f")


def run_cycle():
    """Execute one purge cycle. Called on start and every 24 hours."""
    from conversation_log_agent import process_conversation_log

    _log.info("=== Cycle started ===")
    pst_offset = timedelta(hours=-7)
    now_pst = datetime.now(timezone.utc) + pst_offset

    # T002: Read conversation_log.json
    _log.info("Reading conversation_log.json")
    try:
        with open(CONVERSATION_LOG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        msg = f"Failed to read conversation_log.json: {e}"
        _log.error(msg)
        _write_error(msg)
        return False

    original_count = len(data.get("utterances", []))
    _log.info("Read %d utterances", original_count)

    # T001: Check for records older than 24 hours
    if not _has_old_records(data):
        _log.info("No records older than 24 hours. Skipping cycle.")
        return True

    # T017: Redact credentials
    data = _redact_credentials(data)

    # Read mongoConnectionString and schema
    mongo_conn = _read_mongo_connection_string()
    if not mongo_conn:
        _write_error("mongoConnectionString not available")
        return False

    schema = _read_schema()
    if not schema:
        _write_error("Schema not available")
        return False

    # Calculate preservePastTime (now - 24h)
    preserve_past = (now_pst - timedelta(hours=24)).isoformat()

    # Generate bearerToken
    bearer = f"CH-{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-4{uuid.uuid4().hex[:3]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:12]}"

    # T002: Call agent function
    _log.info("Calling agent with preservePastTime=%s", preserve_past)
    result = process_conversation_log(
        logContent=data,
        bearerToken=bearer,
        mongoConnectionString=mongo_conn,
        preservePastTime=preserve_past,
        schema=json.dumps(schema),
    )

    if result.get("status") != 200:
        msg = f"Agent returned status {result.get('status')}: {result.get('error', '')}"
        _log.error(msg)
        _write_error(msg)
        return False

    _log.info("Agent returned: archived=%d, retained=%d",
              result["counts"]["archived"], result["counts"]["retained"])

    # T012: Build retained file
    retained_file = {
        **result["header"],
        "utterances": result["retained_records"],
    }

    # T020: Inject service utterance
    svc_now_utc = datetime.now(timezone.utc)
    svc_now_pst = svc_now_utc + pst_offset
    next_utt = max((u.get("utterance", 0) for u in retained_file["utterances"]), default=0) + 1
    service_utterance = {
        "utterance": next_utt,
        "userId": "ConversationLogManagerService",
        "role": "service",
        "timestamp_pst": _format_pst(svc_now_pst),
        "timestamp_utc": svc_now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "content": f"Cycle complete. Job {result['jobId']}. "
                   f"{result['counts']['original']} original, "
                   f"{result['counts']['retained']} retained, "
                   f"{result['counts']['archived']} archived.",
        "action": "purge_cycle",
        "outcome": "success",
    }
    retained_file["utterances"].append(service_utterance)

    # T013: Write to .temp
    _log.info("Writing %d utterances to .temp", len(retained_file["utterances"]))
    try:
        with open(TEMP_PATH, "w", encoding="utf-8") as f:
            json.dump(retained_file, f, indent=2, ensure_ascii=False)
    except Exception as e:
        msg = f"Failed to write .temp: {e}"
        _log.error(msg)
        _write_error(msg)
        return False

    # T014: Validate temp file
    try:
        with open(TEMP_PATH, "r", encoding="utf-8") as f:
            validated = json.load(f)
        temp_count = len(validated.get("utterances", []))
    except Exception as e:
        msg = f".temp is not valid JSON: {e}"
        _log.error(msg)
        _write_error(msg)
        TEMP_PATH.unlink(missing_ok=True)
        return False

    # T014: Parity check
    if temp_count > original_count + 2:  # +2 for agent + service utterances
        msg = f"Parity check failed: temp={temp_count} > original={original_count}+2"
        _log.error(msg)
        _write_error(msg)
        TEMP_PATH.unlink(missing_ok=True)
        return False

    # T015: Rename .temp to .json
    try:
        TEMP_PATH.replace(CONVERSATION_LOG_PATH)
        _log.info("Renamed .temp to conversation_log.json (%d utterances)", temp_count)
    except Exception as e:
        msg = f"Failed to rename .temp: {e}"
        _log.error(msg)
        _write_error(msg)
        return False

    # T025: Clean up
    TEMP_PATH.unlink(missing_ok=True)

    _log.info("=== Cycle complete ===")
    return True


def run_loop():
    """T001/B004: Run cycle on start, repeat every 24 hours."""
    _log.info("Service started. Running first cycle now.")
    run_cycle()

    while True:
        _log.info("Sleeping 24 hours until next cycle.")
        time.sleep(86400)
        _log.info("Waking up for next cycle.")
        run_cycle()


# ── Windows service (T001) ──────────────────────────────────────────────

try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager

    class ChatHealthyClaudeLogManagementWindowsService(win32serviceutil.ServiceFramework):
        _svc_name_ = "ChatHealthyClaudeLogMgmt"
        _svc_display_name_ = "ChatHealthy Claude Log Management Service"
        _svc_description_ = "Archives conversation_log.json to MongoDB every 24 hours."

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.running = True

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.running = False
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                 servicemanager.PYS_SERVICE_STARTED,
                                 (self._svc_name_, ""))
            _log.info("Windows service started")

            # Run first cycle immediately
            run_cycle()

            # Then every 24 hours
            while self.running:
                result = win32event.WaitForSingleObject(self.stop_event, 86400000)
                if result == win32event.WAIT_OBJECT_0:
                    break
                run_cycle()

            _log.info("Windows service stopped")

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


if __name__ == "__main__":
    # Add agent module to path
    sys.path.insert(0, str(Path(__file__).resolve().parent))

    if "--purge-now" in sys.argv:
        run_cycle()
    elif HAS_WIN32 and len(sys.argv) > 1 and sys.argv[1] in ("install", "start", "stop", "remove", "restart"):
        win32serviceutil.HandleCommandLine(ChatHealthyClaudeLogManagementWindowsService)
    else:
        print("Usage:")
        print("  python conversation_log_purge_service.py --purge-now    Manual single run")
        print("  python conversation_log_purge_service.py install        Install Windows service")
        print("  python conversation_log_purge_service.py start          Start service")
        print("  python conversation_log_purge_service.py stop           Stop service")
        print("  python conversation_log_purge_service.py remove         Uninstall service")
