# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthyClaudeLogManagementAnthropicAgent
# Agent function for conversation log archival.

import json
import logging
import os
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from pymongo.errors import DuplicateKeyError, PyMongoError

_log = logging.getLogger("ChatHealthyClaudeLogManagementAnthropicAgent")

MONGO_DB = "ClaudeCodeUtterances"
MONGO_COLLECTION = "conversation_log_archive"

REPO_ROOT = Path(os.environ['CHATHEALTHY_PROJECT_ROOT'])

_HEX = frozenset("0123456789abcdefABCDEF")

# CH-xxxxxxxx-xxxx-4xxx-xxxx-xxxxxxxxxxxx: our prefix followed by a version-4
# UUID. Six dash-separated parts of fixed length, the fourth beginning with
# the version digit. Spelled out rather than encoded in a pattern, so the
# shape being accepted is legible without reading a regex.
_CH_GUID_PART_LENGTHS = (8, 4, 4, 4, 12)


def is_ch_guid(value: str) -> bool:
    """True when `value` is one of our CH-prefixed version-4 GUIDs."""
    parts = value.split("-")
    if len(parts) != 6 or parts[0] != "CH":
        return False
    body = parts[1:]
    if [len(p) for p in body] != list(_CH_GUID_PART_LENGTHS):
        return False
    if body[2][0] != "4":
        return False
    return all(ch in _HEX for part in body for ch in part)


def _devops_connection():
    """The DevOps identity, by certificate. Rule-004: no MongoClient here."""
    import sys as _sys, pathlib as _pl
    for _p in _pl.Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "FrontEndApplicationLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            # Hook subprocesses inherit almost nothing, so the vault
            # coordinates have to come from the file rather than the
            # ambient environment.
            from dotenv import load_dotenv as _ld
            _ld(_p / ".env", override=False)
            break
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", "admin")


def process_conversation_log(logContent, bearerToken, mongoConnectionString,
                             preservePastTime, schema):
    """ChatHealthyClaudeLogManagementAnthropicAgent entry point.

    T005: Arguments:
        logContent          - conversation_log.json content (dict or str)
        bearerToken         - CH_GUID authentication token
        mongoConnectionString - MongoDB connection string
        preservePastTime    - PST datetime cutoff (str, ISO or custom format)
        schema              - conversation_log JSON schema (dict or str)

    Returns dict with status, jobId, counts, header, retained_records,
    last_written_ts, temp_file_path, errors.
    """
    job_id = f"JOB-{uuid.uuid4().hex[:12]}"
    _log.info("Job %s started", job_id)

    # ── T003: Validate bearerToken ──────────────────────────────────────
    if not bearerToken or not is_ch_guid(str(bearerToken)):
        _log.warning("Job %s: 401 — invalid bearerToken", job_id)
        return {"status": 401, "error": "Unauthorized", "jobId": job_id}

    # ── T004: Validate required arguments ───────────────────────────────
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

    # ── T005: Extract utterances and header ─────────────────────────────
    utterances = logContent.get("utterances", [])
    header = {k: v for k, v in logContent.items() if k != "utterances"}
    original_count = len(utterances)
    _log.info("Job %s: %d utterances", job_id, original_count)

    # ── T006/T007/T008: Write ALL to MongoDB FIRST ─────────────────────
    mongo_result = _write_to_mongodb(utterances, mongoConnectionString, job_id)

    # ── T010: Query MongoDB for retained records ───────────────────────
    retained_records = _query_retained(mongoConnectionString, cutoff)

    # ── T011: Inject agent utterance ───────────────────────────────────
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

    # ── T009: Return last written timestamp ─────────────────────────────
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


# ── MongoDB write (T006/T007/T008) ─────────────────────────────────────

def _write_to_mongodb(utterances, mongo_conn, job_id):
    errors = []
    archived = 0
    last_written_ts = None

    try:
        client = _devops_connection()
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


# ── MongoDB query for retained records (T010) ──────────────────────────

def _query_retained(mongo_conn, cutoff):
    try:
        client = _devops_connection()
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


# ── Schema validation (B001) ───────────────────────────────────────────

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


# ── Helpers ─────────────────────────────────────────────────────────────

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
    if os.name == "nt":
        return dt.strftime("%#m/%#d/%Y %#H:%M:%S.") + f"{dt.microsecond:06d}"
    return dt.strftime("%-m/%-d/%Y %-H:%M:%S.%f")
