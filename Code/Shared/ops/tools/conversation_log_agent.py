# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BRAIN-CONVERSATION-LOG Agent
# Anthropic tool-use agent for conversation log archival.
# Satisfies requirements B001-B013, T001-T040.
#
# Architecture:
#   - Client uploads logContent to Files API → logContentFileId
#   - Client calls tool via Messages API with logContentFileId + args
#   - Agent downloads file, writes ALL records to MongoDB FIRST
#   - Agent returns success with counts
#   - Client queries MongoDB for records >= preservePastTime
#   - Client rebuilds conversation_log.json from MongoDB query
#   - Client writes to .temp, validates, renames
#
# Usage:
#   python conversation_log_agent.py --test     Full end-to-end test
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
TEMP_PATH = CONVERSATION_LOG_PATH.with_suffix(".json.temp")

AGENT_PERSISTED_FILE = Path(tempfile.gettempdir()) / "conversation_log_job.json"
AGENT_ERROR_LOG = Path(tempfile.gettempdir()) / "conversation_log_agent.log"

MONGO_DB = "ClaudeCodeLog"
MONGO_COLLECTION = "conversation_log_archive"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s — %(message)s")
_log = logging.getLogger("conversation_log_agent")

_anthropic_client = None


def _get_anthropic_client() -> Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        _anthropic_client = Anthropic()
    return _anthropic_client


# ── Validation ──────────────────────────────────────────────────────────

CH_GUID_PATTERN = re.compile(
    r"^CH-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


def _validate_bearer_token(token: str) -> bool:
    return bool(token and CH_GUID_PATTERN.match(token))


def _validate_args(file_id: str, mongo_conn: str, preserve_past: str) -> dict | None:
    if not file_id or not file_id.strip():
        return {"status": 400, "error": "Bad Request", "field": "logContentFileId", "detail": "Empty or missing"}
    if not mongo_conn or not mongo_conn.strip():
        return {"status": 400, "error": "Bad Request", "field": "mongoConnectionString", "detail": "Empty or missing"}
    if not preserve_past or not preserve_past.strip():
        return {"status": 400, "error": "Bad Request", "field": "preservePastTime", "detail": "Empty or missing"}
    try:
        datetime.fromisoformat(preserve_past)
    except (ValueError, TypeError):
        try:
            datetime.strptime(preserve_past, "%m/%d/%Y %H:%M:%S.%f")
        except (ValueError, TypeError):
            return {"status": 400, "error": "Bad Request", "field": "preservePastTime",
                    "detail": f"Cannot parse: {preserve_past}"}
    return None


# ── Helpers ─────────────────────────────────────────────────────────────

def _parse_timestamp_pst(ts: str) -> datetime | None:
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
    try:
        with open(AGENT_ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {message}\n")
    except Exception as e:
        _log.error("Failed to write agent error log: %s", e)


def _download_file(file_id: str) -> str:
    """Download file from Anthropic Files API. Note: only works for files
    created by the API (code_execution output). For uploaded files, content
    must be passed in the message as a document block."""
    client = _get_anthropic_client()
    return client.beta.files.download(file_id).text


def _write_to_mongodb(utterances: list, mongo_conn: str, job_id: str) -> dict:
    """T002/T029: Write ALL records to MongoDB FIRST. Returns counts."""
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
            if last_ts and u_ts and u_ts <= last_ts:
                continue
            try:
                doc = {k: v for k, v in u.items()}
                doc["_archived_by_job"] = job_id
                doc["_archived_at"] = datetime.now(timezone.utc).isoformat()
                col.insert_one(doc)
                archived += 1
            except DuplicateKeyError:
                _log.warning("Duplicate skipped: %s", u.get("timestamp_pst", "?"))
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


# ── The Tool ────────────────────────────────────────────────────────────

@beta_tool
def process_conversation_log(
    logContent: str,
    bearerToken: str,
    mongoConnectionString: str,
    preservePastTime: str,
) -> str:
    """Process the ChatHealthy conversation log for archival.

    Validates authentication, writes ALL records to MongoDB for permanent
    archival, and returns success with counts. The client rebuilds the
    retained file by querying MongoDB for records >= preservePastTime.

    Args:
        logContent: The conversation_log.json content as a JSON string.
        bearerToken: CH_GUID authentication token in format CH-xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx.
        mongoConnectionString: MongoDB connection string for the Frontend cluster.
        preservePastTime: PST datetime cutoff. Records before this time are excluded from the client's rebuilt file.
    """
    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    _log.info("Job %s started", job_id)

    # T016: Validate bearerToken
    if not _validate_bearer_token(bearerToken):
        _log.warning("Job %s: 401 Unauthorized", job_id)
        return json.dumps({"status": 401, "error": "Unauthorized", "jobId": job_id})

    # T018: Validate args
    arg_error = _validate_args("has-content", mongoConnectionString, preservePastTime)
    if arg_error:
        arg_error["jobId"] = job_id
        _log.warning("Job %s: 400 — %s", job_id, arg_error["field"])
        return json.dumps(arg_error)

    # Parse logContent
    try:
        data = json.loads(logContent)
    except Exception as e:
        return json.dumps({"status": 400, "error": "Bad Request", "field": "logContent",
                           "detail": str(e), "jobId": job_id})

    utterances = data.get("utterances", [])
    original_count = len(utterances)
    _log.info("Job %s: %d utterances", job_id, original_count)

    # T019: Persist to local file
    try:
        with open(AGENT_PERSISTED_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception as e:
        _write_agent_error_log(f"Persist failed: {e}")

    # T002/T029: Write ALL records to MongoDB FIRST
    mongo_result = _write_to_mongodb(utterances, mongoConnectionString, job_id)

    # T032: Delete persisted file
    try:
        AGENT_PERSISTED_FILE.unlink(missing_ok=True)
    except Exception:
        pass

    _log.info("Job %s complete: %d original, %d archived, %d errors",
              job_id, original_count, mongo_result["archived"], len(mongo_result["errors"]))

    return json.dumps({
        "status": 200,
        "jobId": job_id,
        "counts": {
            "original": original_count,
            "archived": mongo_result["archived"],
        },
        "errors": mongo_result["errors"] if mongo_result["errors"] else None,
    })


# ── Client-side: rebuild file from MongoDB ──────────────────────────────

def _rebuild_from_mongodb(mongo_conn: str, preserve_past: str, header: dict,
                          original_count: int, schema_str: str, job_id: str) -> bool:
    """Client queries MongoDB for records >= preservePastTime, rebuilds the file.
    Writes to .temp, validates, renames. Returns True on success."""

    cutoff = None
    try:
        cutoff = datetime.fromisoformat(preserve_past)
    except ValueError:
        cutoff = datetime.strptime(preserve_past, "%m/%d/%Y %H:%M:%S.%f")

    # Query MongoDB for retained records
    try:
        client = MongoClient(mongo_conn, serverSelectionTimeoutMS=10000)
        db = client[MONGO_DB]
        col = db[MONGO_COLLECTION]

        # Get all records — we'll filter client-side for consistency
        all_records = list(col.find({}, {"_id": 0, "_archived_by_job": 0, "_archived_at": 0}))
        client.close()
    except PyMongoError as e:
        _log.error("Client: MongoDB query failed: %s", e)
        return False

    # Filter to records >= preservePastTime
    retained = []
    for r in all_records:
        ts = _parse_timestamp_pst(r.get("timestamp_pst", ""))
        if ts is None or ts >= cutoff:
            retained.append(r)

    # Sort by utterance number
    retained.sort(key=lambda u: u.get("utterance", 0))

    # B010: Inject agent utterance
    pst_offset = timedelta(hours=-7)
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + pst_offset
    next_utt = max((u.get("utterance", 0) for u in retained), default=0) + 1

    agent_utterance = {
        "utterance": next_utt,
        "userId": "ConversationLogAgent",
        "role": "agent",
        "timestamp_pst": now_pst.strftime("%#m/%#d/%Y %#H:%M:%S.") + f"{now_pst.microsecond:06d}"
                         if os.name == "nt"
                         else now_pst.strftime("%-m/%-d/%Y %-H:%M:%S.%f"),
        "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "content": f"Archive job {job_id} complete. {original_count} original, "
                   f"{len(retained)} retained from MongoDB.",
        "jobId": job_id,
        "errorLogFileId": None,
        "preservePastTime": preserve_past,
        "counts": {
            "original": original_count,
            "retained": len(retained),
        },
    }
    retained.append(agent_utterance)

    # Build file
    rebuilt = dict(header)
    rebuilt["utterances"] = retained
    rebuilt_json = json.dumps(rebuilt, indent=2, ensure_ascii=False)

    # T004: Write to .temp
    _log.info("Client: Writing %d retained records to .temp", len(retained))
    with open(TEMP_PATH, "w", encoding="utf-8") as f:
        f.write(rebuilt_json)

    # T006: Validate temp is legal JSON
    try:
        with open(TEMP_PATH, "r", encoding="utf-8") as f:
            validated = json.load(f)
        temp_count = len(validated.get("utterances", []))
    except Exception as e:
        _log.error("Client: .temp file is not valid JSON: %s", e)
        TEMP_PATH.unlink(missing_ok=True)
        return False

    # T005: Parity check — retained must be <= original
    if temp_count > original_count + 1:  # +1 for agent utterance
        _log.error("Client: Parity check failed: temp=%d > original=%d+1", temp_count, original_count)
        TEMP_PATH.unlink(missing_ok=True)
        return False

    # T007: Rename .temp to .json
    TEMP_PATH.replace(CONVERSATION_LOG_PATH)
    _log.info("Client: Renamed .temp → conversation_log.json (%d utterances)", temp_count)
    return True


# ── End-to-end test harness ─────────────────────────────────────────────

def _load_env():
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_PATH)
    except ImportError:
        pass


def _run_test(bearer_token: str, log_content: str | None = None,
              preserve_hours: int = 24, expect_status: int = 200):
    """Full end-to-end test: upload → call agent → query MongoDB → rebuild file."""
    _load_env()
    client = Anthropic()

    if log_content is None:
        with open(CONVERSATION_LOG_PATH, "r", encoding="utf-8") as f:
            log_content = f.read()

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema_content = f.read()

    mongo_conn = os.environ.get("MONGO_FRONTEND_connectionString", "")

    pst_offset = timedelta(hours=-7)
    now_pst = datetime.now(timezone.utc) + pst_offset
    cutoff = now_pst - timedelta(hours=preserve_hours)
    preserve_past = cutoff.isoformat()

    # Parse header before sending (client keeps it for rebuild)
    data = json.loads(log_content)
    header = {k: v for k, v in data.items() if k != "utterances"}
    original_count = len(data.get("utterances", []))

    print(f"\n{'='*60}")
    print(f"Testing agent — expect HTTP {expect_status}")
    print(f"  bearerToken: {bearer_token[:20]}...")
    print(f"  preservePastTime: {preserve_past}")
    print(f"  utterances: {original_count}")
    print(f"{'='*60}\n")

    # Build args — logContent passed inline (tool handles it directly)
    args_json = json.dumps({
        "logContent": log_content,
        "bearerToken": bearer_token,
        "mongoConnectionString": mongo_conn,
        "preservePastTime": preserve_past,
    })
    user_msg = f"Call process_conversation_log with these exact arguments:\n{args_json}"

    print("Calling Anthropic Messages API with tool_runner...")
    result = client.beta.messages.tool_runner(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        tools=[process_conversation_log],
        messages=[{"role": "user", "content": user_msg}],
    )

    final = result.until_done()
    print(f"\nAgent response (stop_reason: {final.stop_reason}):")

    agent_result = None
    for block in final.content:
        if hasattr(block, "text"):
            try:
                agent_result = json.loads(block.text)
            except (json.JSONDecodeError, TypeError):
                pass
            print(f"  {block.text[:500]}")

    # If 200, rebuild from MongoDB
    if expect_status == 200 and agent_result and agent_result.get("status") == 200:
        job_id = agent_result.get("jobId", "unknown")
        print(f"\n  Job: {job_id}")
        print(f"  Counts: {agent_result.get('counts', {})}")
        print("\nClient: Rebuilding file from MongoDB...")
        success = _rebuild_from_mongodb(
            mongo_conn, preserve_past, header, original_count, schema_content, job_id
        )
        if success:
            # Verify
            with open(CONVERSATION_LOG_PATH, "r", encoding="utf-8") as f:
                final_data = json.load(f)
            print(f"  Final file: {len(final_data.get('utterances', []))} utterances")
            print("  SUCCESS")
        else:
            print("  FAILED — file not updated")

    return final


def _gen_token() -> str:
    return f"CH-{uuid.uuid4().hex[:8]}-{uuid.uuid4().hex[:4]}-4{uuid.uuid4().hex[:3]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:12]}"


def test_200():
    return _run_test(bearer_token=_gen_token(), expect_status=200)


def test_401():
    return _run_test(bearer_token="bad-token", expect_status=401)


def test_400():
    return _run_test(bearer_token=_gen_token(), log_content="not valid json {{{", expect_status=400)


if __name__ == "__main__":
    if "--test" in sys.argv:
        test_200()
    elif "--test-401" in sys.argv:
        test_401()
    elif "--test-400" in sys.argv:
        test_400()
    else:
        print("Usage:")
        print("  python conversation_log_agent.py --test      Full end-to-end test")
        print("  python conversation_log_agent.py --test-401  Bad token test")
        print("  python conversation_log_agent.py --test-400  Bad args test")
