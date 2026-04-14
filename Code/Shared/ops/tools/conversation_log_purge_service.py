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

import requests

# ── T024: Absolute paths ───────────────────────────────────────────────

REPO_ROOT = Path(r"c:\chatHealthy\findCare")
CONVERSATION_LOG_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"
TEMP_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json.temp"
SCHEMA_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "schema.json"
ENV_PATH = REPO_ROOT / "Code" / ".env"
ERRORS_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "errors.json"
QUEUE_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log_purge_queue.json"
SERVICE_LOG = REPO_ROOT / "test_output" / "conversation_log_service.log"
# Agent code is deployed to Anthropic via skill, not sent in message

# ── Anthropic Managed Agent configuration ──────────────────────────────

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_AGENT_ID = "agent_011Ca3nM1rAxub5dRdEsGLmt"
ANTHROPIC_ENVIRONMENT_ID = "env_01TB8UMfx9MvFUU632wBiPQD"

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


# ── Helper functions (formerly imported from conversation_log_agent) ───

def _parse_datetime(ts):
    if not ts:
        return None
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(str(ts))
    except ValueError:
        pass
    try:
        return datetime.strptime(str(ts), "%m/%d/%Y %H:%M:%S.%f")
    except ValueError:
        pass
    try:
        return datetime.strptime(str(ts), "%m/%d/%Y %H:%M:%S")
    except ValueError:
        pass
    return None


def _validate_against_schema(data, schema):
    """B001: Validate utterances against the conversation log schema."""
    violations = []
    try:
        prompt_log = schema.get("collections", {}).get("prompt_log", {})
        record_schemas = prompt_log.get("record_schemas", {})
        log_record = record_schemas.get("LogRecord", {})
        fields = log_record.get("fields", {})
        if not fields:
            return violations
        for u in data.get("utterances", []):
            uid = u.get("utterance", "?")
            for fname, fdef in fields.items():
                if fdef.get("required") and fname not in u:
                    violations.append(f"utterance:{uid} missing '{fname}'")
                if fname in u:
                    possible = fdef.get("possible_values", [])
                    val = u[fname]
                    if possible and val and isinstance(val, str) and val not in possible:
                        violations.append(f"utterance:{uid} '{fname}'='{val}' not in {possible}")
    except Exception as e:
        violations.append(f"Schema validation error: {e}")
    return violations


def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass


def _read_anthropic_api_key():
    _load_env()
    key = os.environ.get("Anthropic_API_KEY", "")
    if not key:
        _log.error("Anthropic_API_KEY not found in environment")
    return key


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
    pst_offset = timedelta(hours=-7)
    cutoff = (datetime.now(timezone.utc) + pst_offset - timedelta(hours=24)).replace(tzinfo=None)
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


# ── Anthropic Managed Agent API call ──────────────────────────────────

def _call_anthropic_agent(data, bearer, mongo_conn, preserve_past, schema_json):
    """Call the ChatHealthyClaudeLogManagementAnthropicAgent on Anthropic Managed Agents."""
    api_key = _read_anthropic_api_key()
    if not api_key:
        return {"status": 500, "error": "Anthropic API key not available"}

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "managed-agents-2026-04-01",
        "content-type": "application/json",
    }

    # Step 1: Upload payload as a file to Anthropic
    payload = {
        "logContent": data,
        "bearerToken": bearer,
        "mongoConnectionString": mongo_conn,
        "preservePastTime": preserve_past,
        "schema": schema_json,
    }
    payload_bytes = json.dumps(payload).encode("utf-8")

    _log.info("Uploading payload to Anthropic Files API (%d bytes)", len(payload_bytes))
    try:
        resp = requests.post(
            f"{ANTHROPIC_API_URL}/v1/files",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "files-api-2025-04-14",
            },
            files=[("file", ("payload.json", payload_bytes, "application/json"))],
            timeout=60,
        )
        resp.raise_for_status()
        file_id = resp.json()["id"]
        _log.info("Payload uploaded: %s", file_id)
    except Exception as e:
        return {"status": 500, "error": f"Failed to upload payload: {e}"}

    # Step 2: Create a session
    _log.info("Creating Anthropic managed agent session")
    try:
        resp = requests.post(
            f"{ANTHROPIC_API_URL}/v1/sessions",
            headers=headers,
            json={
                "agent": ANTHROPIC_AGENT_ID,
                "environment_id": ANTHROPIC_ENVIRONMENT_ID,
                "title": f"Log archival {datetime.now(timezone.utc).isoformat()}",
            },
            timeout=60,
        )
        resp.raise_for_status()
        session = resp.json()
        session_id = session["id"]
        _log.info("Session created: %s", session_id)
    except Exception as e:
        return {"status": 500, "error": f"Failed to create session: {e}"}

    # Step 3: Send message — the agent code is in the skill, data is in the uploaded file
    message_text = (
        "A payload file has been uploaded to the container. Do exactly these steps:\n\n"
        "1. Run: pip install pymongo dnspython -q\n"
        "2. The agent code is at /workspace/skills/conversation-log-archival/conversation_log_agent.py — "
        "copy it to /tmp/conversation_log_agent.py\n"
        "3. Read the payload from /uploads/payload.json\n"
        "4. Write and execute /tmp/runner.py:\n\n"
        "```python\n"
        "import json, sys\n"
        "sys.path.insert(0, '/tmp')\n"
        "from conversation_log_agent import process_conversation_log\n\n"
        "with open('/uploads/payload.json', 'r') as f:\n"
        "    payload = json.load(f)\n\n"
        "result = process_conversation_log(\n"
        "    logContent=payload['logContent'],\n"
        "    bearerToken=payload['bearerToken'],\n"
        "    mongoConnectionString=payload['mongoConnectionString'],\n"
        "    preservePastTime=payload['preservePastTime'],\n"
        "    schema=payload['schema'],\n"
        ")\n"
        "print('===AGENT_RESULT_START===')\n"
        "print(json.dumps(result, default=str))\n"
        "print('===AGENT_RESULT_END===')\n"
        "```\n\n"
        "5. Show me ONLY the output between the markers. No commentary."
    )

    _log.info("Sending message to agent session %s", session_id)
    try:
        resp = requests.post(
            f"{ANTHROPIC_API_URL}/v1/sessions/{session_id}/events",
            headers=headers,
            json={
                "events": [{
                    "type": "user.message",
                    "content": [
                        {"type": "text", "text": message_text},
                        {
                            "type": "document",
                            "source": {"type": "file", "file_id": file_id},
                            "title": "payload.json",
                        },
                    ],
                }],
            },
            timeout=60,
        )
        resp.raise_for_status()
    except Exception as e:
        return {"status": 500, "error": f"Failed to send message: {e}"}

    # Step 4: Stream events and collect the agent's response
    _log.info("Streaming agent response from session %s", session_id)
    full_text = ""
    try:
        resp = requests.get(
            f"{ANTHROPIC_API_URL}/v1/sessions/{session_id}/stream",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "agent-api-2026-03-01",
                "Accept": "text/event-stream",
            },
            stream=True,
            timeout=600,
        )
        resp.raise_for_status()

        for line in resp.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            json_str = line[6:]
            try:
                event = json.loads(json_str)
            except json.JSONDecodeError:
                continue

            event_type = event.get("type", "")
            if event_type == "agent.message":
                for block in event.get("content", []):
                    if block.get("type") == "text":
                        full_text += block.get("text", "")
            elif event_type == "session.status_idle":
                _log.info("Agent session idle — processing complete")
                break
            elif event_type == "error":
                return {"status": 500, "error": f"Agent error: {event.get('error', '')}"}

    except Exception as e:
        return {"status": 500, "error": f"Failed to stream response: {e}"}

    # Step 5: Extract the JSON result from the agent's response
    start_marker = "===AGENT_RESULT_START==="
    end_marker = "===AGENT_RESULT_END==="
    start_idx = full_text.find(start_marker)
    end_idx = full_text.find(end_marker)

    if start_idx == -1 or end_idx == -1:
        _log.error("Agent response missing result markers. Response length: %d", len(full_text))
        _log.error("Response tail: %s", full_text[-500:] if len(full_text) > 500 else full_text)
        return {"status": 500, "error": "Agent response missing result markers"}

    result_json = full_text[start_idx + len(start_marker):end_idx].strip()
    try:
        result = json.loads(result_json)
        return result
    except json.JSONDecodeError as e:
        _log.error("Failed to parse agent result JSON: %s", e)
        return {"status": 500, "error": f"Failed to parse agent result: {e}"}


# ── Main cycle ─────────────────────────────────────────────────────────

def run_cycle():
    """Execute one purge cycle."""
    try:
        return _run_cycle_inner()
    except Exception as e:
        _log.error("Cycle crashed: %s", e)
        import traceback
        _log.error(traceback.format_exc())
        for h in _log.handlers:
            h.flush()
        _write_error(f"Cycle crashed: {e}")
        return False


def _run_cycle_inner():
    _log.info("=== Cycle started ===")
    pst_offset = timedelta(hours=-7)
    now_pst = (datetime.now(timezone.utc) + pst_offset).replace(tzinfo=None)

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

    # Normalize all timestamps to naive (strip timezone) before any processing.
    for u in data.get("utterances", []):
        for field in ("timestamp_pst", "timestamp_utc"):
            val = u.get(field, "")
            if val:
                dt = _parse_datetime(val)
                if dt and dt.tzinfo is not None:
                    u[field] = dt.replace(tzinfo=None).isoformat()

    # B005: Archive ALL records to MongoDB every cycle (B wins over T001)
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

    # T002: Call Anthropic managed agent
    _log.info("Calling Anthropic agent with preservePastTime=%s", preserve_past)
    result = _call_anthropic_agent(
        data=data,
        bearer=bearer,
        mongo_conn=mongo_conn,
        preserve_past=preserve_past,
        schema_json=json.dumps(schema),
    )

    if result.get("status") != 200:
        msg = f"Agent returned status {result.get('status')}: {result.get('error', '')}"
        _log.error(msg)
        _write_error(msg)  # T023: Write error on agent failure
        return False

    _log.info("Agent returned: archived=%d, retained=%d",
              result["counts"]["archived"], result["counts"]["retained"])

    # T012: Service builds the retained file from agent results
    retained_file = {
        **result["header"],
        "utterances": result["retained_records"],
    }

    # B009: Inject service utterance
    svc_now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
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
        "actor": "ConversationLogManagerService",
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

    # T013: Service writes retained file to .temp
    _log.info("Writing %d utterances to .temp", len(retained_file["utterances"]))
    try:
        with open(TEMP_PATH, "w", encoding="utf-8") as f:
            json.dump(retained_file, f, indent=2, ensure_ascii=False)
    except Exception as e:
        msg = f"Failed to write .temp: {e}"
        _log.error(msg)
        _write_error(msg)  # T023
        return False

    # T014: Service validates .temp is legal JSON
    try:
        with open(TEMP_PATH, "r", encoding="utf-8") as f:
            validated = json.load(f)
    except Exception as e:
        msg = f".temp is not valid JSON: {e}"
        _log.error(msg)
        _write_error(msg)  # T023
        TEMP_PATH.unlink(missing_ok=True)
        return False

    # T014: Parity check — every record in .temp must be within 24 hours
    now_check = (datetime.now(timezone.utc) + pst_offset).replace(tzinfo=None)
    cutoff = now_check - timedelta(hours=24)
    _log.info("Parity check: now=%s, cutoff (24h ago)=%s", now_check.isoformat(), cutoff.isoformat())

    for u in validated.get("utterances", []):
        if u.get("role") in ("agent", "service"):
            continue
        ts = _parse_datetime(u.get("timestamp_pst", ""))
        if ts and ts < cutoff:
            diff = cutoff - ts
            hours, remainder = divmod(int(diff.total_seconds()), 3600)
            minutes = remainder // 60
            msg = f"Parity check failed: record {u.get('ch_key', '?')} timestamp {u.get('timestamp_pst')} is older than 24 hours (now={now_check.isoformat()}, cutoff={cutoff.isoformat()}, record is {hours}h {minutes}m before cutoff)"
            _log.error(msg)
            _write_error(msg)
            TEMP_PATH.unlink(missing_ok=True)
            return False

    _log.info("Parity check passed: %d utterances, all within 24 hours", len(validated.get("utterances", [])))

    # B001: Schema validation
    violations = _validate_against_schema(validated, schema)
    if violations:
        msg = f"Parity check failed: schema violations: {violations}"
        _log.error(msg)
        _write_error(msg)  # T023
        TEMP_PATH.unlink(missing_ok=True)
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
