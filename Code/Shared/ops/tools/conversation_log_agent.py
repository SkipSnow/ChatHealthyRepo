# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BRAIN-CONVERSATION-LOG Agent
# Anthropic tool-use agent for conversation log archival.
# Satisfies requirements B001-B013, T001-T040.
#
# Usage:
#   python conversation_log_agent.py --test     Run test against real conversation_log.json
#   python conversation_log_agent.py --test-401 Test bad bearerToken → 401
#   python conversation_log_agent.py --test-400 Test missing arg → 400

import json
import logging
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from anthropic import Anthropic, beta_tool
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

# ── Paths (T033, T034) ──────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parents[4]
CONVERSATION_LOG_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"
SCHEMA_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "schema.json"
ENV_PATH = REPO_ROOT / "Code" / ".env"
ERRORS_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "errors.json"
QUEUE_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log_purge_queue.json"

# Agent-side paths (T034) — ephemeral per session
AGENT_PERSISTED_FILE = Path(tempfile.gettempdir()) / "conversation_log_job.json"
AGENT_ERROR_LOG = Path(tempfile.gettempdir()) / "conversation_log_agent.log"

# ── MongoDB (B008) ──────────────────────────────────────────────────────

MONGO_DB = "ClaudeCodeLog"
MONGO_COLLECTION = "conversation_log_archive"

# ── Logging ─────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("conversation_log_agent")

# ── Shared Anthropic client (used by tool for Files API) ────────────────

_anthropic_client = None


def _get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic()
    return _anthropic_client


# ── CH_GUID validation (T013, T016) ─────────────────────────────────────

CH_GUID_PATTERN = re.compile(
    r"^CH-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_bearer_token(token: str) -> bool:
    """T013: Validate CH_GUID format."""
    return bool(token and CH_GUID_PATTERN.match(token))


def _validate_args(log_content_file_id: str, mongo_conn: str,
                   preserve_past: str, schema_str: str) -> dict | None:
    """T018: Validate all args. Returns error dict or None if valid."""
    # logContentFileId must be non-empty
    if not log_content_file_id or not log_content_file_id.strip():
        return {"status": 400, "error": "Bad Request", "field": "logContentFileId",
                "detail": "Empty or missing"}

    # mongoConnectionString must be non-empty
    if not mongo_conn or not mongo_conn.strip():
        return {"status": 400, "error": "Bad Request", "field": "mongoConnectionString",
                "detail": "Empty or missing"}

    # preservePastTime must be parseable as a datetime
    if not preserve_past or not preserve_past.strip():
        return {"status": 400, "error": "Bad Request", "field": "preservePastTime",
                "detail": "Empty or missing"}
    try:
        datetime.fromisoformat(preserve_past)
    except (ValueError, TypeError):
        try:
            datetime.strptime(preserve_past, "%m/%d/%Y %H:%M:%S.%f")
        except (ValueError, TypeError):
            return {"status": 400, "error": "Bad Request", "field": "preservePastTime",
                    "detail": f"Cannot parse datetime: {preserve_past}"}

    # schema must be valid JSON
    try:
        json.loads(schema_str)
    except (json.JSONDecodeError, TypeError):
        return {"status": 400, "error": "Bad Request", "field": "schema",
                "detail": "Not valid JSON"}

    return None


def _parse_preserve_past_time(preserve_past: str) -> datetime:
    """Parse preservePastTime from either ISO or custom PST format."""
    try:
        return datetime.fromisoformat(preserve_past)
    except ValueError:
        return datetime.strptime(preserve_past, "%m/%d/%Y %H:%M:%S.%f")


def _parse_timestamp_pst(ts: str) -> datetime | None:
    """Parse a timestamp_pst value from an utterance."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        pass
    try:
        return datetime.strptime(ts, "%m/%d/%Y %H:%M:%S.%f")
    except ValueError:
        pass
    try:
        return datetime.strptime(ts, "%m/%d/%Y %H:%M:%S")
    except ValueError:
        pass
    return None


def _write_agent_error_log(message: str):
    """T034: Write to agent-side error log."""
    try:
        with open(AGENT_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {message}\n")
    except Exception as e:
        _log.error("Failed to write agent error log: %s", e)


def _download_file(file_id: str) -> str:
    """T019: Download file content from Anthropic Files API."""
    client = _get_anthropic_client()
    response = client.beta.files.download(file_id)
    return response.text


def _upload_file(content: str, filename: str) -> str:
    """Upload content to Anthropic Files API, return file_id."""
    client = _get_anthropic_client()
    response = client.beta.files.upload(
        file=(filename, content.encode("utf-8"), "application/json"),
    )
    return response.id


def _write_to_mongodb(utterances: list, mongo_conn: str, job_id: str) -> dict:
    """T029-T031: Write ALL records to MongoDB. MongoDB FIRST (T002/T029).
    Returns counts dict with archived count and any errors."""
    errors = []
    archived = 0

    try:
        client = MongoClient(mongo_conn, serverSelectionTimeoutMS=10000)
        db = client[MONGO_DB]
        col = db[MONGO_COLLECTION]

        # T031: Query most recent timestamp_pst
        last_doc = col.find_one(sort=[("timestamp_pst", -1)])
        last_ts = None
        if last_doc:
            last_ts = _parse_timestamp_pst(last_doc.get("timestamp_pst", ""))

        for u in utterances:
            u_ts = _parse_timestamp_pst(u.get("timestamp_pst", ""))

            # T031: Only insert records newer than last stored
            if last_ts and u_ts and u_ts <= last_ts:
                continue

            try:
                doc = {k: v for k, v in u.items()}
                doc["_archived_by_job"] = job_id
                doc["_archived_at"] = datetime.now(timezone.utc).isoformat()
                col.insert_one(doc)
                archived += 1
            except DuplicateKeyError:
                # T027: Skip duplicate, log warning, continue
                _log.warning("Duplicate timestamp_pst skipped: %s", u.get("timestamp_pst", "?"))
                _write_agent_error_log(f"WARN: Duplicate key skipped: {u.get('timestamp_pst', '?')}")
            except PyMongoError as e:
                msg = f"MongoDB insert error: {e}"
                _log.error(msg)
                _write_agent_error_log(msg)
                errors.append(msg)

        client.close()

    except PyMongoError as e:
        msg = f"MongoDB connection error: {e}"
        _log.error(msg)
        _write_agent_error_log(msg)
        errors.append(msg)

    return {"archived": archived, "errors": errors}


def _validate_against_schema(data: dict, schema_str: str) -> list:
    """T021/T028: Validate JSON against schema. Returns list of violations."""
    violations = []
    try:
        schema = json.loads(schema_str)
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
                    violations.append(f"utterance:{uid} missing required field '{fname}'")
                if fname in u:
                    possible = fdef.get("possible_values", [])
                    val = u[fname]
                    if possible and val and isinstance(val, str) and val not in possible:
                        violations.append(f"utterance:{uid} '{fname}' value '{val}' not in {possible}")
    except Exception as e:
        violations.append(f"Schema validation error: {e}")

    return violations


def _upload_error_log() -> str | None:
    """T035: Upload error log to Anthropic Files API. Returns file_id or None."""
    if not AGENT_ERROR_LOG.exists():
        return None
    try:
        content = AGENT_ERROR_LOG.read_text(encoding="utf-8")
        if not content.strip():
            return None
        return _upload_file(content, "conversation_log_agent_error.log")
    except Exception as e:
        _log.error("Failed to upload error log: %s", e)
        return None


# ── The Tool (all 53 requirements) ──────────────────────────────────────

@beta_tool
def process_conversation_log(
    logContentFileId: str,
    bearerToken: str,
    mongoConnectionString: str,
    preservePastTime: str,
    schema: str,
    errorLogFileId: str = "",
) -> str:
    """Process the ChatHealthy conversation log for archival.

    Downloads the conversation log from Anthropic Files API, separates retained
    records from excluded records based on preservePastTime, writes ALL records
    to MongoDB for permanent archival, uploads the retained file to Files API,
    and returns the retainedFileId for the client to download.

    Args:
        logContentFileId: Anthropic Files API file_id of the uploaded conversation_log.json.
        bearerToken: CH_GUID authentication token in format CH-xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx.
        mongoConnectionString: MongoDB connection string for the Frontend cluster.
        preservePastTime: PST datetime cutoff. Records before this time are excluded. Records at or after are included.
        schema: The conversation_log JSON schema as a JSON string.
        errorLogFileId: Anthropic Files API file_id of the previous error log. Empty string on first run.
    """
    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    _log.info("Job %s started", job_id)

    # ── T035: Download previous error log if exists ─────────────────────
    if errorLogFileId and errorLogFileId.strip():
        try:
            prev_errors = _download_file(errorLogFileId)
            with open(AGENT_ERROR_LOG, "w", encoding="utf-8") as f:
                f.write(prev_errors)
            _log.info("Job %s: Downloaded previous error log", job_id)
        except Exception as e:
            _log.warning("Job %s: Could not download previous error log: %s", job_id, e)

    # ── T016: Validate bearerToken ──────────────────────────────────────
    if not _validate_bearer_token(bearerToken):
        _log.warning("Job %s: 401 Unauthorized — bad bearerToken", job_id)
        return json.dumps({"status": 401, "error": "Unauthorized", "jobId": job_id})

    # ── T018: Validate all args ─────────────────────────────────────────
    arg_error = _validate_args(logContentFileId, mongoConnectionString, preservePastTime, schema)
    if arg_error:
        arg_error["jobId"] = job_id
        _log.warning("Job %s: 400 Bad Request — %s: %s", job_id, arg_error["field"], arg_error["detail"])
        return json.dumps(arg_error)

    # ── T019: Download logContent from Files API and persist locally ─────
    try:
        log_content = _download_file(logContentFileId)
        with open(AGENT_PERSISTED_FILE, "w", encoding="utf-8") as f:
            f.write(log_content)
        _log.info("Job %s: Downloaded and persisted logContent (%d chars)", job_id, len(log_content))
    except Exception as e:
        msg = f"Failed to download logContent from Files API: {e}"
        _log.error("Job %s: %s", job_id, msg)
        _write_agent_error_log(msg)
        return json.dumps({"status": 500, "error": "Internal Server Error", "detail": msg, "jobId": job_id})

    # ── T020: Parse header from utterances ──────────────────────────────
    try:
        data = json.loads(log_content)
    except Exception as e:
        msg = f"logContent is not valid JSON: {e}"
        _log.error("Job %s: %s", job_id, msg)
        _write_agent_error_log(msg)
        return json.dumps({"status": 400, "error": "Bad Request", "field": "logContentFileId",
                           "detail": msg, "jobId": job_id})

    utterances = data.get("utterances", [])
    header = {k: v for k, v in data.items() if k != "utterances"}
    original_count = len(utterances)

    _log.info("Job %s: %d utterances, header keys: %s", job_id, original_count, list(header.keys()))

    # ── T017/T022/T026: Separate retained from excluded ─────────────────
    cutoff = _parse_preserve_past_time(preservePastTime)
    retained = []
    for u in utterances:
        ts = _parse_timestamp_pst(u.get("timestamp_pst", ""))
        if ts is None or ts >= cutoff:
            retained.append(u)

    retained_count = len(retained)
    _log.info("Job %s: %d retained, %d excluded", job_id, retained_count, original_count - retained_count)

    # ── T002/T029: Write ALL records to MongoDB FIRST ───────────────────
    mongo_result = _write_to_mongodb(utterances, mongoConnectionString, job_id)

    # ── B010: Build agent utterance and inject into retained file ────────
    pst_offset = timedelta(hours=-7)
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + pst_offset
    next_utterance = max((u.get("utterance", 0) for u in retained), default=0) + 1

    # T035: Upload error log, get file_id
    new_error_log_file_id = _upload_error_log()

    agent_utterance = {
        "utterance": next_utterance,
        "userId": "ConversationLogAgent",
        "role": "agent",
        "timestamp_pst": now_pst.strftime("%#m/%#d/%Y %#H:%M:%S.") + f"{now_pst.microsecond:06d}"
                         if os.name == "nt"
                         else now_pst.strftime("%-m/%-d/%Y %-H:%M:%S.%f"),
        "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "content": f"Archive job {job_id} complete. {original_count} original, "
                   f"{retained_count} retained, {mongo_result['archived']} archived to MongoDB.",
        "jobId": job_id,
        "errorLogFileId": new_error_log_file_id,
        "preservePastTime": preservePastTime,
        "counts": {
            "original": original_count,
            "retained": retained_count,
            "archived": mongo_result["archived"],
        },
    }
    if mongo_result["errors"]:
        agent_utterance["error"] = "; ".join(mongo_result["errors"])

    retained.append(agent_utterance)

    # ── T021/T026/T028: Build retained file JSON ────────────────────────
    response_data = dict(header)
    response_data["utterances"] = retained

    # T028: Validate against schema
    violations = _validate_against_schema(response_data, schema)
    if violations:
        _log.warning("Job %s: Schema violations: %s", job_id, violations)
        _write_agent_error_log(f"Schema violations: {violations}")

    retained_json = json.dumps(response_data, indent=2, ensure_ascii=False)

    # ── T029 step 3: Upload retained file to Files API ──────────────────
    try:
        retained_file_id = _upload_file(retained_json, "conversation_log_retained.json")
        _log.info("Job %s: Uploaded retained file → %s", job_id, retained_file_id)
    except Exception as e:
        msg = f"Failed to upload retained file to Files API: {e}"
        _log.error("Job %s: %s", job_id, msg)
        _write_agent_error_log(msg)
        # Fall back to returning the file inline
        return json.dumps({
            "status": 200,
            "retainedFileId": None,
            "retained_file_inline": retained_json,
            "jobId": job_id,
            "error": msg,
        }, ensure_ascii=False)

    # ── T032: Delete persisted file ─────────────────────────────────────
    try:
        AGENT_PERSISTED_FILE.unlink(missing_ok=True)
    except Exception as e:
        _log.warning("Job %s: Failed to delete persisted file: %s", job_id, e)

    _log.info("Job %s complete: original=%d, retained=%d, archived=%d, errors=%d",
              job_id, original_count, len(retained), mongo_result["archived"],
              len(mongo_result["errors"]))

    # ── T029 step 6: Return retainedFileId to client ────────────────────
    return json.dumps({
        "status": 200,
        "retainedFileId": retained_file_id,
        "jobId": job_id,
        "counts": {
            "original": original_count,
            "retained": len(retained),
            "archived": mongo_result["archived"],
        },
    })


# ── Test harness ────────────────────────────────────────────────────────

def _load_env():
    """Load .env for API keys."""
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass


def _run_agent_test(bearer_token: str, log_content: str | None = None,
                    preserve_hours: int = 24, expect_status: int = 200):
    """Run the agent via Anthropic Messages API tool_runner.
    Uploads logContent to Files API first, then calls the tool with the file_id."""
    _load_env()
    client = Anthropic()

    # Read real data if not provided
    if log_content is None:
        with open(CONVERSATION_LOG_PATH, "r", encoding="utf-8") as f:
            log_content = f.read()

    # Read schema
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_content = f.read()

    # Read mongo connection string from .env
    mongo_conn = os.environ.get("MONGO_FRONTEND_connectionString", "")

    # Calculate preservePastTime
    pst_offset = timedelta(hours=-7)  # PDT
    now_pst = datetime.now(timezone.utc) + pst_offset
    cutoff = now_pst - timedelta(hours=preserve_hours)
    preserve_past = cutoff.isoformat()

    # T033/T015: Upload logContent to Files API → get logContentFileId
    print("Uploading logContent to Anthropic Files API...")
    upload_response = client.beta.files.upload(
        file=("conversation_log.json", log_content.encode("utf-8"), "application/json"),
    )
    log_content_file_id = upload_response.id
    print(f"  logContentFileId: {log_content_file_id}")

    # Build the user message with file_id reference
    args_json = json.dumps({
        "logContentFileId": log_content_file_id,
        "bearerToken": bearer_token,
        "mongoConnectionString": mongo_conn,
        "preservePastTime": preserve_past,
        "schema": schema_content,
        "errorLogFileId": "",
    })
    user_msg = f"Call process_conversation_log with these exact arguments:\n{args_json}"

    print(f"\n{'='*60}")
    print(f"Testing agent — expect HTTP {expect_status}")
    print(f"  bearerToken: {bearer_token[:20]}...")
    print(f"  preservePastTime: {preserve_past}")
    print(f"  logContentFileId: {log_content_file_id}")
    print(f"{'='*60}\n")

    # Use tool_runner — handles the agentic loop internally
    result = client.beta.messages.tool_runner(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=[process_conversation_log],
        messages=[{"role": "user", "content": user_msg}],
    )

    final = result.until_done()

    print(f"\nFinal response (stop_reason: {final.stop_reason}):")
    for block in final.content:
        if hasattr(block, "text"):
            text = block.text
            try:
                parsed = json.loads(text)
                print(f"  Status: {parsed.get('status', '?')}")
                print(f"  Job ID: {parsed.get('jobId', '?')}")
                if parsed.get("retainedFileId"):
                    print(f"  retainedFileId: {parsed['retainedFileId']}")
                if parsed.get("counts"):
                    print(f"  Counts: {parsed['counts']}")
                if parsed.get("error"):
                    print(f"  Error: {parsed['error']}")
            except (json.JSONDecodeError, TypeError):
                print(f"  {text[:1000]}")

    return final


def test_200():
    """Test normal operation with real data."""
    token = f"CH-{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-4{uuid.uuid4().hex[:3]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:12]}"
    return _run_agent_test(bearer_token=token, expect_status=200)


def test_401():
    """Test bad bearerToken → 401."""
    return _run_agent_test(bearer_token="bad-token", expect_status=401)


def test_400():
    """Test missing/bad arg → 400."""
    return _run_agent_test(
        bearer_token=f"CH-{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-4{uuid.uuid4().hex[:3]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:12]}",
        log_content="not valid json {{{",
        expect_status=400,
    )


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_200()
    elif "--test-401" in sys.argv:
        test_401()
    elif "--test-400" in sys.argv:
        test_400()
    else:
        print("Usage:")
        print("  python conversation_log_agent.py --test      Normal operation test")
        print("  python conversation_log_agent.py --test-401  Bad token test")
        print("  python conversation_log_agent.py --test-400  Bad args test")
