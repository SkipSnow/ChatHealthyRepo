# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthyClaudeLogManagementWindowsService
# Windows service for conversation log archival.
# Agent logic merged into service — single local worker.
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

from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# ── T024: Absolute paths ───────────────────────────────────────────────

REPO_ROOT = Path(r"c:\chatHealthy\findCare")
CONVERSATION_LOG_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"
TEMP_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json.temp"
SCHEMA_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "schema.json"
ENV_PATH = REPO_ROOT / "Code" / ".env"
ERRORS_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "errors.json"
QUEUE_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log_purge_queue.json"
SERVICE_LOG = REPO_ROOT / "test_output" / "conversation_log_service.log"

MONGO_DB = "ClaudeCodeLog"
MONGO_COLLECTION = "conversation_log_archive"

CH_GUID_PATTERN = re.compile(
    r"^CH-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

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


# ── Helpers ────────────────────────────────────────────────────────────

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


def _format_pst(dt):
    """B003: Format PST timestamp with microsecond precision."""
    if os.name == "nt":
        return dt.strftime("%#m/%#d/%Y %#H:%M:%S.") + f"{dt.microsecond:06d}"
    return dt.strftime("%-m/%-d/%Y %-H:%M:%S.%f")


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


def _redact_credentials(data):
    """T017: Redact security tokens from utterance content before processing."""
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


# ── Schema validation (B001) ──────────────────────────────────────────

def _validate_against_schema(data, schema):
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


# ── MongoDB write (T006/T007/T008) ────────────────────────────────────

def _write_to_mongodb(utterances, mongo_conn, job_id):
    errors = []
    archived = 0
    last_written_ts = None

    try:
        client = MongoClient(mongo_conn, serverSelectionTimeoutMS=10000)
        db = client[MONGO_DB]

        # Ensure collection and indexes exist
        if MONGO_COLLECTION not in db.list_collection_names():
            db.create_collection(MONGO_COLLECTION)
            _log.info("Created collection %s.%s", MONGO_DB, MONGO_COLLECTION)
        col = db[MONGO_COLLECTION]
        existing_indexes = {idx["name"] for idx in col.list_indexes()}
        if "ch_key_1" not in existing_indexes:
            col.create_index("ch_key", unique=True)
            _log.info("Created unique index on ch_key")
        if "timestamp_pst_1" not in existing_indexes:
            col.create_index("timestamp_pst")
            _log.info("Created index on timestamp_pst")
        if "timestamp_utc_1" not in existing_indexes:
            col.create_index("timestamp_utc")
            _log.info("Created index on timestamp_utc")

        # T007: Query most recent timestamp_pst
        last_doc = col.find_one(sort=[("timestamp_pst", -1)])
        last_ts = None
        if last_doc:
            last_ts = _parse_datetime(last_doc.get("timestamp_pst", ""))

        # Build batch of new records only
        now_archived = datetime.now(timezone.utc).isoformat()
        batch = []
        for u in utterances:
            u_ts = _parse_datetime(u.get("timestamp_pst", ""))
            if last_ts and u_ts and u_ts <= last_ts:
                continue
            doc = {k: v for k, v in u.items()}
            doc["_archived_by_job"] = job_id
            doc["_archived_at"] = now_archived
            batch.append(doc)

        # T006: batch insert_many with ordered=False
        if batch:
            try:
                result = col.insert_many(batch, ordered=False)
                archived = len(result.inserted_ids)
            except PyMongoError as e:
                # T008: BulkWriteError — count partial success, skip dups
                if hasattr(e, "details"):
                    archived = e.details.get("nInserted", 0)
                    for err in e.details.get("writeErrors", []):
                        if err.get("code") == 11000:
                            _log.warning("Duplicate skipped: %s", err.get("errmsg", "")[:80])
                        else:
                            msg = f"MongoDB batch error: {err.get('errmsg', '')}"
                            _log.error(msg)
                            errors.append(msg)
                else:
                    msg = f"MongoDB batch error: {e}"
                    _log.error(msg)
                    errors.append(msg)

        # T009: Get last written timestamp
        last_written = col.find_one(sort=[("timestamp_pst", -1)])
        if last_written:
            last_written_ts = last_written.get("timestamp_pst")

        client.close()

    except PyMongoError as e:
        msg = f"MongoDB connection error: {e}"
        _log.error(msg)
        errors.append(msg)

    return {"archived": archived, "errors": errors, "last_written_ts": last_written_ts}


# ── MongoDB query for retained records (T010) ─────────────────────────

def _query_retained(mongo_conn, cutoff):
    try:
        client = MongoClient(mongo_conn, serverSelectionTimeoutMS=10000)
        db = client[MONGO_DB]
        col = db[MONGO_COLLECTION]

        all_records = list(col.find(
            {},
            {"_id": 0, "_archived_by_job": 0, "_archived_at": 0}
        ).sort("timestamp_pst", 1))
        client.close()

        retained = []
        for r in all_records:
            ts = _parse_datetime(r.get("timestamp_pst", ""))
            if ts is None or ts >= cutoff:
                retained.append(r)
        return retained

    except PyMongoError as e:
        _log.error("MongoDB query error: %s", e)
        return []


# ── Agent function (T003-T011) ─────────────────────────────────────────

def _process_conversation_log(logContent, bearerToken, mongoConnectionString,
                              preservePastTime, schema):
    """Archive utterances to MongoDB and return retained records."""
    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    _log.info("Job %s started", job_id)

    # T003: Validate bearerToken
    if not bearerToken or not CH_GUID_PATTERN.match(str(bearerToken)):
        _log.warning("Job %s: 401 — invalid bearerToken", job_id)
        return {"status": 401, "error": "Unauthorized", "jobId": job_id}

    # T004: Validate required arguments
    if not logContent:
        return {"status": 400, "error": "Bad Request", "field": "logContent", "jobId": job_id}
    if not mongoConnectionString:
        return {"status": 400, "error": "Bad Request", "field": "mongoConnectionString", "jobId": job_id}
    if not preservePastTime:
        return {"status": 400, "error": "Bad Request", "field": "preservePastTime", "jobId": job_id}
    if not schema:
        return {"status": 400, "error": "Bad Request", "field": "schema", "jobId": job_id}

    # Parse logContent if string
    if isinstance(logContent, str):
        try:
            logContent = json.loads(logContent)
        except json.JSONDecodeError as e:
            return {"status": 400, "error": "Bad Request", "field": "logContent",
                    "detail": str(e), "jobId": job_id}

    # Parse schema if string
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as e:
            return {"status": 400, "error": "Bad Request", "field": "schema",
                    "detail": str(e), "jobId": job_id}

    # Parse preservePastTime
    cutoff = _parse_datetime(preservePastTime)
    if cutoff is None:
        return {"status": 400, "error": "Bad Request", "field": "preservePastTime",
                "detail": f"Cannot parse: {preservePastTime}", "jobId": job_id}

    # T005: Extract utterances and header
    utterances = logContent.get("utterances", [])
    header = {k: v for k, v in logContent.items() if k != "utterances"}
    original_count = len(utterances)
    _log.info("Job %s: %d utterances", job_id, original_count)

    # T006/T007/T008: Write ALL to MongoDB FIRST
    mongo_result = _write_to_mongodb(utterances, mongoConnectionString, job_id)

    # T010: Query MongoDB for retained records
    retained_records = _query_retained(mongoConnectionString, cutoff)

    # T011: Inject agent utterance
    pst_offset = timedelta(hours=-7)
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + pst_offset
    next_utt = max((u.get("utterance", 0) for u in retained_records), default=0) + 1

    # B007: Stagger timestamp if collision
    agent_ts = now_pst
    existing_timestamps = {u.get("timestamp_pst", "") for u in retained_records}
    formatted = _format_pst(agent_ts)
    while formatted in existing_timestamps:
        agent_ts = agent_ts + timedelta(microseconds=1)
        formatted = _format_pst(agent_ts)
    agent_utc = now_utc + (agent_ts - now_pst)

    agent_utterance = {
        "ch_key": f"CH_KEY{{{uuid.uuid4()}}}",
        "utterance": next_utt,
        "actor": "ConversationLogAgent",
        "role": "agent",
        "timestamp_pst": formatted,
        "timestamp_utc": agent_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "content": f"Archive job {job_id} complete. {original_count} original, "
                   f"{len(retained_records)} retained, {mongo_result['archived']} archived.",
        "jobId": job_id,
        "preservePastTime": preservePastTime,
        "counts": {
            "original": original_count,
            "retained": len(retained_records),
            "archived": mongo_result["archived"],
        },
    }
    if mongo_result["errors"]:
        agent_utterance["error"] = "; ".join(mongo_result["errors"])

    retained_records.append(agent_utterance)

    _log.info("Job %s complete: original=%d, retained=%d, archived=%d, errors=%d",
              job_id, original_count, len(retained_records),
              mongo_result["archived"], len(mongo_result["errors"]))

    return {
        "status": 200,
        "jobId": job_id,
        "header": header,
        "retained_records": retained_records,
        "last_written_ts": mongo_result["last_written_ts"],
        "counts": {
            "original": original_count,
            "retained": len(retained_records),
            "archived": mongo_result["archived"],
        },
        "errors": mongo_result["errors"] if mongo_result["errors"] else None,
    }


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

    # T002: Call agent function (merged into service)
    _log.info("Calling agent with preservePastTime=%s", preserve_past)
    result = _process_conversation_log(
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
