# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Conversation Log Consumer (PID 2)
# Reads from Kafka, redacts (credentials + profanity), assigns CH_KEY_<GUID>,
# adds timestamps, archives to MongoDB. Manual commit after batch write.
# Batches writes to MongoDB (insert_many). Flush on 50 messages or 30s.

import json
import logging
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from confluent_kafka import Consumer, KafkaError
from pymongo import MongoClient
from pymongo.errors import PyMongoError

PROJECT_ROOT = Path(__file__).resolve().parents[2]

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / "Code" / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
_log = logging.getLogger("conversation_log_consumer")

KAFKA_BOOTSTRAP = os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
KAFKA_TOPIC = os.environ.get("KAFKA_TOPIC", "conversation-log")
KAFKA_GROUP = os.environ.get("KAFKA_GROUP_ID", "conversation-log-consumer")
MONGO_CONN = os.environ.get("MONGO_FRONTEND_connectionString", "")
MONGO_DB = "ClaudeCodeLog"
MONGO_COLLECTION = "conversation_log_archive"
BATCH_SIZE = 50
BATCH_TIMEOUT_SECONDS = 30

ENV_FILE_PATH = PROJECT_ROOT / "Code" / ".env"
ERRORS_PATH = PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "errors.json"
ERROR_LOG_PATH = Path(tempfile.gettempdir()) / "chathealthy_consumer_errors.log"

CREDENTIALS_REDACTION_RETRIES = 3


def _write_error(msg):
    """Write error to errors.json (legacy path)."""
    try:
        errors = []
        if ERRORS_PATH.exists():
            errors = json.loads(ERRORS_PATH.read_text(encoding="utf-8"))
        errors.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "ConversationLogConsumer",
            "error": str(msg)[:500],
        })
        ERRORS_PATH.write_text(json.dumps(errors, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _append_consumer_error_log(mongo_id: str, error_text: str) -> None:
    """Append one JSONL line to the consumer error log keyed by mongo_id."""
    try:
        entry = {
            "timestamp_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "mongo_id": str(mongo_id),
            "error": str(error_text)[:500],
        }
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        _log.warning("Failed to write to consumer error log: %s", e)


def _make_timestamps():
    """PST with 0.1s precision + UTC."""
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + timedelta(hours=-7)
    return (
        now_pst.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_pst.microsecond // 100000}" + "-07:00",
        now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


# ── Redaction helpers ────────────────────────────────────────────────────────


_env_content_cache: str | None = None


def _get_env_content() -> str:
    """Read Code/.env once and cache for inclusion in credentials redaction prompt."""
    global _env_content_cache
    if _env_content_cache is None:
        if ENV_FILE_PATH.exists():
            _env_content_cache = ENV_FILE_PATH.read_text(encoding="utf-8", errors="replace")
        else:
            _env_content_cache = ""
    return _env_content_cache


def _get_prior_utterances(mongo_client, before_ts: str, n: int = 5) -> list:
    """Query Mongo for N most recent utterances strictly before the given timestamp."""
    try:
        coll = mongo_client[MONGO_DB][MONGO_COLLECTION]
        cursor = (
            coll.find({"timestamp_utc": {"$lt": before_ts}}, {"actor": 1, "content": 1, "_id": 0})
                .sort("timestamp_utc", -1)
                .limit(n)
        )
        return list(cursor)
    except Exception:
        return []


def _redact_credentials(content, oai_client, mongo_client, before_ts):
    """Credentials redaction with .env + last-5-utterances context. Retries up to 3 times.
    Returns (redacted_content, error_or_none). On all failures, returns original content
    plus a terse error string for caller to record."""
    if not content or len(content) < 5:
        return content, None
    if not oai_client:
        return content, "openai-client-unavailable"

    env_content = _get_env_content()
    prior_utterances = _get_prior_utterances(mongo_client, before_ts) if mongo_client else []

    if prior_utterances:
        prior_section = "\n".join(
            json.dumps({"actor": u.get("actor", "?"), "content": u.get("content", "")}, ensure_ascii=False)
            for u in prior_utterances
        )
    else:
        prior_section = "(no prior utterances)"

    system_prompt = (
        "You are a credentials redaction engine. Return the SAME text with API keys, "
        "passwords, connection strings, tokens, secrets, and credentials replaced "
        "with [Sensitive content redacted]. Leave all other text exactly as-is. "
        "Do not summarize or rephrase.\n\n"
        f"Reference Code/.env file (non-exhaustive — secrets may not be here):\n"
        f"```\n{env_content}\n```\n\n"
        f"Last {len(prior_utterances)} utterances for context:\n"
        f"```\n{prior_section}\n```"
    )

    last_error = None
    for attempt in range(1, CREDENTIALS_REDACTION_RETRIES + 1):
        try:
            response = oai_client.chat.completions.create(
                model="gpt-4.1-mini",
                temperature=0,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": content},
                ],
            )
            return response.choices[0].message.content, None
        except Exception as e:
            last_error = f"attempt-{attempt}: {e}"
            if attempt < CREDENTIALS_REDACTION_RETRIES:
                time.sleep(2 ** (attempt - 1))
    _log.warning("Credentials redaction failed after %d attempts: %s — storing unredacted",
                 CREDENTIALS_REDACTION_RETRIES, last_error)
    return content, last_error


def _redact_profanity(content, oai_client):
    """Profanity redaction with single LLM attempt. Fail-open returns original content."""
    if not content or len(content) < 5:
        return content
    if not oai_client:
        return content
    try:
        response = oai_client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[
                {"role": "system", "content": (
                    "You are a profanity redaction engine. Return the SAME text with "
                    "profanity and vulgar language replaced with [expletive deleted]. "
                    "Leave all other text exactly as-is. Do not summarize or rephrase."
                )},
                {"role": "user", "content": content},
            ],
        )
        return response.choices[0].message.content
    except Exception as e:
        _log.warning("Profanity redaction failed: %s — storing unredacted", e)
        return content


# ── Payload parsing + record build ───────────────────────────────────────────


def _parse_hook_payload(raw_bytes):
    """Parse raw payload from hook. Falls back to {"raw": ...} on parse failure."""
    try:
        return json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {"raw": raw_bytes.decode("utf-8", errors="replace")}


def _build_record(payload, headers, oai_client, mongo_client):
    """Build a MongoDB record from a Kafka message.

    Returns (record_dict_or_None, credentials_error_or_None).
    credentials_error is set when credentials redaction exhausted retries;
    caller MUST append a consumer-error-log entry referencing the inserted
    mongo_id after insert_many returns.
    """
    msg_build = 0
    msg_version = "?"
    msg_framework = "?"
    for h in (headers or []):
        key = h[0]
        val = h[1].decode("utf-8") if isinstance(h[1], bytes) else str(h[1])
        if key == "_build":
            try:
                msg_build = int(val)
            except (TypeError, ValueError):
                msg_build = 0
        elif key == "_version":
            msg_version = val
        elif key == "_framework":
            msg_framework = val

    pst, utc = _make_timestamps()

    prompt = payload.get("prompt", "")
    transcript_path = payload.get("transcript_path", "")

    actor = "Unknown"
    role = "unknown"
    content = ""

    if prompt:
        actor = "Operator"
        role = "user"
        content = prompt
    elif transcript_path:
        try:
            if os.path.exists(transcript_path):
                with open(transcript_path, encoding="utf-8") as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            if entry.get("type") == "assistant" and entry.get("message"):
                                msg = entry["message"]
                                if isinstance(msg.get("content"), list):
                                    text_parts = [
                                        block.get("text", "")
                                        for block in msg["content"]
                                        if block.get("type") == "text"
                                    ]
                                    content = " ".join(text_parts).strip()
                                elif isinstance(msg.get("content"), str):
                                    content = msg["content"]
                                actor = "Claude"
                                role = "assistant"
                        except (json.JSONDecodeError, KeyError):
                            continue
        except Exception as e:
            _log.warning("Failed to read transcript: %s", e)
    else:
        content = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        actor = "System"
        role = "system"

    if not content:
        return None, None

    # Credentials redaction first (with .env + 5 prior utterances). Then profanity.
    content, credentials_error = _redact_credentials(content, oai_client, mongo_client, utc)
    content = _redact_profanity(content, oai_client)

    record = {
        "ch_key": f"CH_KEY_{uuid.uuid4()}",
        "timestamp_pst": pst,
        "timestamp_utc": utc,
        "actor": actor,
        "role": role,
        "content": content,
        "_version": msg_version,
        "_framework": msg_framework,
        "_build": msg_build,
    }
    return record, credentials_error


# ── Consumer loop ────────────────────────────────────────────────────────────


def run_consumer():
    """Main consumer loop. Reads from Kafka, batches, writes to MongoDB."""
    _log.info("Starting consumer: bootstrap=%s topic=%s group=%s",
              KAFKA_BOOTSTRAP, KAFKA_TOPIC, KAFKA_GROUP)

    if not MONGO_CONN:
        _log.error("MONGO_FRONTEND_connectionString not set")
        _write_error("Consumer cannot start: MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    try:
        from openai import OpenAI
        oai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    except Exception as e:
        _log.warning("OpenAI client init failed: %s — redaction disabled", e)
        oai_client = None

    # Long-lived Mongo client for prior-utterance lookups during redaction
    mongo_client = MongoClient(MONGO_CONN, serverSelectionTimeoutMS=10000)

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": KAFKA_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
        "max.poll.interval.ms": 300000,
    })
    consumer.subscribe([KAFKA_TOPIC])
    _log.info("Subscribed to topic: %s", KAFKA_TOPIC)

    batch = []
    redaction_errors: dict[str, str] = {}  # ch_key -> credentials error text
    last_flush = time.time()

    try:
        while True:
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                if batch and (time.time() - last_flush) >= BATCH_TIMEOUT_SECONDS:
                    _flush_batch(batch, redaction_errors, consumer, mongo_client)
                    batch = []
                    redaction_errors = {}
                    last_flush = time.time()
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                _log.error("Kafka error: %s", msg.error())
                continue

            try:
                payload = _parse_hook_payload(msg.value())
                record, credentials_error = _build_record(payload, msg.headers(), oai_client, mongo_client)
                if record:
                    batch.append(record)
                    if credentials_error:
                        redaction_errors[record["ch_key"]] = credentials_error
                    _log.debug("Batch: %d messages", len(batch))
            except Exception as e:
                _log.error("Failed to process message: %s", e)
                _write_error(f"Consumer process error: {e}")

            if len(batch) >= BATCH_SIZE:
                _flush_batch(batch, redaction_errors, consumer, mongo_client)
                batch = []
                redaction_errors = {}
                last_flush = time.time()

    except KeyboardInterrupt:
        _log.info("Consumer shutting down")
    finally:
        if batch:
            _flush_batch(batch, redaction_errors, consumer, mongo_client)
        consumer.close()
        mongo_client.close()
        _log.info("Consumer closed")


def _flush_batch(batch, redaction_errors, consumer, mongo_client):
    """Write batch to MongoDB using insert_many. Commit Kafka offset after success.
    For each inserted record whose ch_key has an entry in redaction_errors,
    append a line to the consumer error log keyed by the returned ObjectId."""
    if not batch:
        return

    _log.info("Flushing %d records to MongoDB", len(batch))

    try:
        coll = mongo_client[MONGO_DB][MONGO_COLLECTION]

        existing_keys = set()
        for doc in coll.find(
            {"ch_key": {"$in": [r["ch_key"] for r in batch]}},
            {"ch_key": 1}
        ):
            existing_keys.add(doc["ch_key"])

        new_records = [r for r in batch if r["ch_key"] not in existing_keys]

        if new_records:
            result = coll.insert_many(new_records, ordered=False)
            _log.info("Archived %d records (%d duplicates skipped)",
                      len(new_records), len(batch) - len(new_records))
            # Per REQ-T-003: append consumer-error-log entry for each record whose
            # credentials redaction failed, using the ObjectId returned by Mongo.
            for inserted_id, record in zip(result.inserted_ids, new_records):
                err = redaction_errors.get(record["ch_key"])
                if err:
                    _append_consumer_error_log(inserted_id, err)
        else:
            _log.info("All %d records already in MongoDB (duplicates)", len(batch))

        consumer.commit(asynchronous=False)
        _log.info("Committed offset")

    except PyMongoError as e:
        _log.error("MongoDB write failed: %s — will retry on next flush", e)
        _write_error(f"MongoDB batch write failed: {e}")


if __name__ == "__main__":
    run_consumer()
