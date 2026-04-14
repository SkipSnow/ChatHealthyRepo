# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ChatHealthyClaudeLogManagementWindowsService
# Single-file conversation log archival service.
# T011: main constructs ConversationLogService, calls run().

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
from pymongo.errors import PyMongoError

# ── Paths ──────────────────────────────────────────────────────────────

REPO_ROOT = Path(r"c:\chatHealthy\findCare")
CONVERSATION_LOG_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"
TEMP_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json.temp"
ENV_PATH = REPO_ROOT / "Code" / ".env"
ERRORS_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "errors.json"
VERSION_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "version.json"
SERVICE_LOG = REPO_ROOT / "test_output" / "conversation_log_service.log"

MONGO_DB = "ClaudeCodeLog"
MONGO_COLLECTION = "conversation_log_archive"

CH_GUID_PATTERN = re.compile(
    r"^CH-[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-4[0-9a-fA-F]{3}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# B004: Redaction patterns
_CREDENTIAL_PATTERNS = [
    re.compile(r"(sk-ant-api\S+)", re.IGNORECASE),
    re.compile(r"(mongodb\+srv://[^\s\"']+)", re.IGNORECASE),
    re.compile(r"(ANTHROPIC_API_KEY\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(MONGO_FRONTEND_connectionString\s*=\s*\S+)", re.IGNORECASE),
    re.compile(r"(password\s*[:=]\s*\S+)", re.IGNORECASE),
    re.compile(r"(api[_-]?key\s*[:=]\s*\S+)", re.IGNORECASE),
]
_PROFANITY_PATTERNS = [
    re.compile(r"\b(fuck)\b", re.IGNORECASE),
    re.compile(r"\b(shit)\b", re.IGNORECASE),
    re.compile(r"\b(damn)\b", re.IGNORECASE),
    re.compile(r"\b(ass)\b", re.IGNORECASE),
    re.compile(r"\b(bitch)\b", re.IGNORECASE),
]

# B007: Cycle intervals by working_mode
_CYCLE_INTERVALS = {
    "idiot": None,       # prompt operator
    "normal": 7200,      # 2 hours
    "unattended": 3600,  # 1 hour
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    handlers=[
        logging.FileHandler(SERVICE_LOG, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)


class ConversationLogService:
    """T011: All business logic lives here. main constructs this and calls run()."""

    def __init__(self):
        self._log = logging.getLogger("ChatHealthyClaudeLogManagementWindowsService")
        self._load_env()
        self._mongo_conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        self._version_info = self._read_version()
        self._working_mode = self._version_info.get("current", {}).get("working_mode", "normal")
        self._log.info("Service initialized. working_mode=%s", self._working_mode)

    # ── Public ─────────────────────────────────────────────────────────

    def run(self):
        """B007/B010: Run cycles based on working_mode. Loops until stopped."""
        self._log.info("Service started. working_mode=%s", self._working_mode)

        while True:
            # B007: idiot mode = prompt operator
            if self._working_mode == "idiot":
                try:
                    answer = input("Run archive cycle now? (y/n): ").strip().lower()
                    if answer != "y":
                        self._log.info("Operator declined cycle. Waiting 60s before asking again.")
                        time.sleep(60)
                        continue
                except EOFError:
                    self._log.info("No interactive input (running as service). Falling back to normal mode.")
                    self._working_mode = "normal"
                    continue

            self._run_cycle()

            interval = _CYCLE_INTERVALS.get(self._working_mode, 7200)
            if interval is None:
                interval = 60  # idiot mode fallback
            self._log.info("Next cycle in %d seconds (%s mode)", interval, self._working_mode)
            time.sleep(interval)

    # ── Cycle ──────────────────────────────────────────────────────────

    def _run_cycle(self):
        try:
            self._run_cycle_inner()
        except Exception as e:
            self._log.error("Cycle crashed: %s", e)
            import traceback
            self._log.error(traceback.format_exc())
            for h in self._log.handlers:
                h.flush()
            self._write_error(f"Cycle crashed: {e}")

    def _run_cycle_inner(self):
        self._log.info("=== Cycle started ===")
        pst_offset = timedelta(hours=-7)
        now_pst = (datetime.now(timezone.utc) + pst_offset).replace(tzinfo=None)

        # T006: Lock, copy, release within 2s SLA
        lock_start = time.monotonic()
        lock_handle = self._acquire_lock(CONVERSATION_LOG_PATH)
        if lock_handle is None:
            return

        self._log.info("Reading conversation_log.json")
        try:
            lock_handle.seek(0)
            data = json.load(lock_handle)
        except Exception as e:
            self._log.error("Failed to read conversation_log.json: %s", e)
            self._write_error(f"Failed to read conversation_log.json: {e}")
            self._release_lock(lock_handle)
            return

        self._release_lock(lock_handle)
        lock_elapsed = time.monotonic() - lock_start
        self._log.info("Lock-copy-release in %.3fs (SLA: 2s)", lock_elapsed)

        original_count = len(data.get("utterances", []))
        self._log.info("Read %d utterances", original_count)

        # Normalize timestamps to naive
        for u in data.get("utterances", []):
            for field in ("timestamp_pst", "timestamp_utc"):
                val = u.get(field, "")
                if val:
                    dt = _parse_datetime(val)
                    if dt and dt.tzinfo is not None:
                        u[field] = dt.replace(tzinfo=None).isoformat()

        # T003: Add CH_KEY{GUID} to every utterance
        for u in data.get("utterances", []):
            if "ch_key" not in u:
                u["ch_key"] = f"CH_KEY{{{uuid.uuid4()}}}"

        # B004: Redact credentials and profanity
        self._redact(data)

        # T009: Read mongo connection
        if not self._mongo_conn:
            self._write_error("mongoConnectionString not available")
            return

        # T001: Read schema
        schema = self._read_schema(data)
        if not schema:
            self._write_error("Schema not available")
            return

        # B003: Archive ALL to MongoDB
        preserve_past = (now_pst - timedelta(hours=24)).isoformat()
        job_id = f"JOB-{uuid.uuid4().hex[:12]}"
        self._log.info("Job %s: archiving %d utterances", job_id, original_count)

        utterances = data.get("utterances", [])
        mongo_result = self._write_to_mongodb(utterances, job_id)

        # B005: Query retained (last 24h) and trim local file
        cutoff = _parse_datetime(preserve_past)
        retained = self._query_retained(cutoff)

        # T008: Inject service utterance
        next_utt = max((u.get("utterance", 0) for u in retained), default=0) + 1
        svc_ts = (datetime.now(timezone.utc) + pst_offset).replace(tzinfo=None)
        service_utterance = {
            "ch_key": f"CH_KEY{{{uuid.uuid4()}}}",
            "utterance": next_utt,
            "actor": "ConversationLogManagerService",
            "role": "service",
            "timestamp_pst": _format_pst(svc_ts),
            "timestamp_utc": datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "content": f"Cycle complete. Job {job_id}. "
                       f"{original_count} original, "
                       f"{len(retained)} retained, "
                       f"{mongo_result['archived']} archived.",
            "action": "purge_cycle",
            "outcome": "success",
        }
        retained.append(service_utterance)

        self._log.info("Job %s complete: original=%d, retained=%d, archived=%d, errors=%d",
                       job_id, original_count, len(retained),
                       mongo_result["archived"], len(mongo_result["errors"]))

        # B005: Write trimmed file
        header = {k: v for k, v in data.items() if k != "utterances"}
        retained_file = {**header, "utterances": retained}

        try:
            with open(CONVERSATION_LOG_PATH, "w", encoding="utf-8") as f:
                json.dump(retained_file, f, indent=2, ensure_ascii=False)
            self._log.info("Wrote %d utterances to conversation_log.json", len(retained))
        except Exception as e:
            self._log.error("Failed to write conversation_log.json: %s", e)
            self._write_error(f"Failed to write conversation_log.json: {e}")
            return

        self._log.info("=== Cycle complete ===")

    # ── MongoDB ────────────────────────────────────────────────────────

    def _write_to_mongodb(self, utterances, job_id):
        errors = []
        archived = 0

        try:
            client = MongoClient(self._mongo_conn, serverSelectionTimeoutMS=10000)
            db = client[MONGO_DB]

            if MONGO_COLLECTION not in db.list_collection_names():
                db.create_collection(MONGO_COLLECTION)
                self._log.info("Created collection %s.%s", MONGO_DB, MONGO_COLLECTION)
            col = db[MONGO_COLLECTION]

            existing_indexes = {idx["name"] for idx in col.list_indexes()}
            if "ch_key_1" not in existing_indexes:
                col.create_index("ch_key", unique=True)
                self._log.info("Created unique index on ch_key")
            if "timestamp_pst_1" not in existing_indexes:
                col.create_index("timestamp_pst")
            if "timestamp_utc_1" not in existing_indexes:
                col.create_index("timestamp_utc")

            # T003: Assign stable ch_key based on content fingerprint
            # The service generates the GUID, but it must be stable across cycles.
            # Fingerprint = hash(utterance + timestamp_pst + first 100 chars of content)
            import hashlib
            existing_keys = set(
                doc["ch_key"] for doc in col.find({}, {"ch_key": 1, "_id": 0})
            )
            self._log.info("MongoDB has %d existing records", len(existing_keys))

            version = self._version_info.get("current", {})
            now_archived = datetime.now(timezone.utc).isoformat()
            batch = []
            for u in utterances:
                # Generate deterministic ch_key from content
                fingerprint = f"{u.get('utterance', '')}-{u.get('timestamp_pst', '')}-{str(u.get('content', ''))[:100]}"
                stable_key = f"CH_KEY{{{uuid.uuid5(uuid.NAMESPACE_DNS, fingerprint)}}}"
                u["ch_key"] = stable_key

                if stable_key in existing_keys:
                    continue
                doc = {k: v for k, v in u.items()}
                doc["_archived_by_job"] = job_id
                doc["_archived_at"] = now_archived
                doc["_build"] = version.get("build")
                doc["_version"] = version.get("version")
                doc["_framework"] = version.get("framework")
                batch.append(doc)

            if batch:
                try:
                    result = col.insert_many(batch, ordered=False)
                    archived = len(result.inserted_ids)
                except PyMongoError as e:
                    if hasattr(e, "details"):
                        archived = e.details.get("nInserted", 0)
                        for err in e.details.get("writeErrors", []):
                            if err.get("code") == 11000:
                                self._log.warning("Duplicate skipped: %s", err.get("errmsg", "")[:80])
                            else:
                                errors.append(f"MongoDB batch error: {err.get('errmsg', '')}")
                    else:
                        errors.append(f"MongoDB batch error: {e}")

            client.close()

        except PyMongoError as e:
            msg = f"MongoDB connection error: {e}"
            self._log.error(msg)
            errors.append(msg)

        return {"archived": archived, "errors": errors}

    def _query_retained(self, cutoff):
        try:
            client = MongoClient(self._mongo_conn, serverSelectionTimeoutMS=10000)
            db = client[MONGO_DB]
            col = db[MONGO_COLLECTION]

            all_records = list(col.find(
                {},
                {"_id": 0, "_archived_by_job": 0, "_archived_at": 0,
                 "_build": 0, "_version": 0, "_framework": 0}
            ).sort("timestamp_pst", 1))
            client.close()

            retained = []
            for r in all_records:
                ts = _parse_datetime(r.get("timestamp_pst", ""))
                if ts is None or ts >= cutoff:
                    retained.append(r)
            return retained

        except PyMongoError as e:
            self._log.error("MongoDB query error: %s", e)
            return []

    # ── Helpers ─────────────────────────────────────────────────────────

    def _load_env(self):
        try:
            from dotenv import load_dotenv
            load_dotenv(ENV_PATH)
        except ImportError:
            pass

    def _read_version(self):
        try:
            with open(VERSION_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._log.error("Failed to read version.json: %s", e)
            return {"current": {"version": "0.0.0", "framework": "0.0.0", "build": 0, "working_mode": "normal"}}

    def _read_schema(self, data):
        schema_address = data.get("schema_address", "brain/machine_artifacts/content/schema.json")
        schema_path = REPO_ROOT / schema_address
        try:
            with open(schema_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            self._log.error("Failed to read schema from %s: %s", schema_path, e)
            return None

    def _redact(self, data):
        """B004: Redact credentials and profanity."""
        for u in data.get("utterances", []):
            content = u.get("content", "")
            if isinstance(content, str):
                for pattern in _CREDENTIAL_PATTERNS:
                    content = pattern.sub("[Sensitive content redacted]", content)
                for pattern in _PROFANITY_PATTERNS:
                    content = pattern.sub("[expletive redacted]", content)
                u["content"] = content

    def _acquire_lock(self, file_path):
        """T006: Acquire file lock. Retry every 5s for up to 10 minutes."""
        for attempt in range(120):
            try:
                f = open(file_path, "r", encoding="utf-8")
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                return f
            except (IOError, OSError):
                if attempt < 119:
                    time.sleep(5)
                else:
                    self._log.error("File locked for 10 minutes: %s", file_path)
                    self._write_error(f"File locked for 10 minutes: {file_path}")
                    return None

    def _release_lock(self, f):
        if f:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except (IOError, OSError):
                pass
            f.close()

    def _write_error(self, message):
        """T007: Write error to errors.json."""
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
            self._log.error("Failed to write to errors.json: %s", e)


# ── Module-level helpers (stateless) ───────────────────────────────────

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
    """B002: Format PST timestamp with 0.1 second precision."""
    if os.name == "nt":
        return dt.strftime("%#m/%#d/%Y %#H:%M:%S.") + f"{dt.microsecond // 100000}"
    return dt.strftime("%-m/%-d/%Y %-H:%M:%S.") + f"{dt.microsecond // 100000}"


# ── T011: main ─────────────────────────────────────────────────────────

def main():
    service = ConversationLogService()
    service.run()


if __name__ == "__main__":
    main()
