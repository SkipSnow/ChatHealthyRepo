# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthyClaudeLogManagementWindowsService
# Windows service for conversation log archival.
#
# Usage:
#   python conversation_log_purge_service.py --purge-now

import os
import sys
from pathlib import Path

_THIS_DIR = str(Path(__file__).resolve().parent)
if _THIS_DIR not in sys.path:
    sys.path.insert(0, _THIS_DIR)

import json
import logging
import msvcrt
import re
import time
import uuid
from datetime import datetime, timezone, timedelta

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

# T017: Credential patterns for redaction
_CREDENTIAL_PATTERNS = [
    re.compile(r"(sk-ant-api\S+)", re.IGNORECASE),
    re.compile(r"(mongodb\+srv://[^\s\"']+)", re.IGNORECASE),
    re.compile(r"(ANTHROPIC_API_KEY\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(MONGO_FRONTEND_connectionString\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*\S+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*\S+)", re.IGNORECASE),
]


def _load_env():
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


def _read_schema_from_header(data):
    """T020: Read schema from the address in conversation_log.json header."""
    schema_address = data.get("schema_address", "")
    if not schema_address:
        _log.warning("No schema_address in conversation_log.json header, falling back to default")
        schema_address = "brain/machine_artifacts/content/schema.json"
    schema_path = REPO_ROOT / schema_address
    try:
        with open(schema_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        _log.error("Failed to read schema from %s: %s", schema_path, e)
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


def _acquire_lock(file_path):
    """T019: Open file with write lock, retry 5s x 120 (10 minutes)."""
    for attempt in range(120):
        try:
            f = open(file_path, "r", encoding="utf-8")
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
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
        except (IOError, OSError):
            pass
        f.close()


def _write_error(message):
    """T023: Write error to errors.json."""
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
    """Execute one purge cycle."""
    from conversation_log_agent import process_conversation_log

    _log.info("=== Cycle started ===")
    pst_offset = timedelta(hours=-7)
    now_pst = datetime.now(timezone.utc) + pst_offset

    # T019: Acquire lock on conversation_log.json
    lock_handle = _acquire_lock(CONVERSATION_LOG_PATH)
    if lock_handle is None:
        return False

    # T002/T016: Read conversation_log.json (original stays intact until rename)
    _log.info("Reading conversation_log.json")
    try:
        lock_handle.seek(0)
        data = json.load(lock_handle)
    except Exception as e:
        msg = f"Failed to read conversation_log.json: {e}"
        _log.error(msg)
        _write_error(msg)
        _release_lock(lock_handle)
        return False

    # T019: Release lock after reading
    _release_lock(lock_handle)

    original_count = len(data.get("utterances", []))
    _log.info("Read %d utterances", original_count)

    # T001: Check for records older than 24 hours
    if not _has_old_records(data):
        _log.info("No records older than 24 hours. Skipping cycle.")
        return True

    # T027: Add CH_KEY{GUID} to every utterance
    for u in data.get("utterances", []):
        u["ch_key"] = f"CH_KEY{{{uuid.uuid4()}}}"

    # T017: Redact credentials
    data = _redact_credentials(data)

    # T005: Read mongoConnectionString from Code/.env
    mongo_conn = _read_mongo_connection_string()
    if not mongo_conn:
        _write_error("mongoConnectionString not available")
        return False

    # T020: Read schema from address in header
    schema = _read_schema_from_header(data)
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
        _write_error(msg)  # T023: Write error on agent failure
        return False

    _log.info("Agent returned: archived=%d, retained=%d",
              result["counts"]["archived"], result["counts"]["retained"])

    # T012: The agent already built the retained file and wrote .temp (T013).
    # The agent already validated .temp (T014).
    # Now inject service utterance into the .temp file.
    try:
        with open(TEMP_PATH, "r", encoding="utf-8") as f:
            retained_file = json.load(f)
    except Exception as e:
        msg = f"Failed to read .temp after agent: {e}"
        _log.error(msg)
        _write_error(msg)  # T023
        return False

    # B009: Inject service utterance
    svc_now_utc = datetime.now(timezone.utc)
    svc_now_pst = svc_now_utc + pst_offset
    next_utt = max((u.get("utterance", 0) for u in retained_file["utterances"]), default=0) + 1

    # B007: Stagger timestamp if collision
    svc_ts = svc_now_pst
    existing_timestamps = {u.get("timestamp_pst", "") for u in retained_file["utterances"]}
    formatted = _format_pst(svc_ts)
    while formatted in existing_timestamps:
        svc_ts = svc_ts + timedelta(microseconds=1)
        formatted = _format_pst(svc_ts)
    svc_utc = svc_now_utc + (svc_ts - svc_now_pst)

    service_utterance = {
        "ch_key": f"CH_KEY{{{uuid.uuid4()}}}",
        "utterance": next_utt,
        "userId": "ConversationLogManagerService",
        "role": "service",
        "timestamp_pst": formatted,
        "timestamp_utc": svc_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "content": f"Cycle complete. Job {result['jobId']}. "
                   f"{result['counts']['original']} original, "
                   f"{result['counts']['retained']} retained, "
                   f"{result['counts']['archived']} archived.",
        "action": "purge_cycle",
        "outcome": "success",
    }
    retained_file["utterances"].append(service_utterance)

    # T012: Rewrite .temp with service utterance added
    _log.info("Writing %d utterances to .temp", len(retained_file["utterances"]))
    try:
        with open(TEMP_PATH, "w", encoding="utf-8") as f:
            json.dump(retained_file, f, indent=2, ensure_ascii=False)
    except Exception as e:
        msg = f"Failed to rewrite .temp: {e}"
        _log.error(msg)
        _write_error(msg)  # T023
        return False

    # T015: Rename .temp to .json (T019: acquire lock first)
    lock_handle = _acquire_lock(CONVERSATION_LOG_PATH)
    if lock_handle is None:
        _write_error("Cannot acquire lock for rename")  # T023
        return False

    _release_lock(lock_handle)  # Release read lock, then rename

    try:
        TEMP_PATH.replace(CONVERSATION_LOG_PATH)
        _log.info("Renamed .temp to conversation_log.json (%d utterances)",
                  len(retained_file["utterances"]))
    except Exception as e:
        msg = f"Failed to rename .temp: {e}"
        _log.error(msg)
        _write_error(msg)  # T023
        return False

    # T025: Clean up
    TEMP_PATH.unlink(missing_ok=True)

    _log.info("=== Cycle complete ===")
    return True


if __name__ == "__main__":
    if "--purge-now" in sys.argv:
        run_cycle()
    else:
        print("Usage:")
        print("  python conversation_log_purge_service.py --purge-now    Manual single run")
