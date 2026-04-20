# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Conversation Log Consumer (PID 2)
# T003: Reads from Kafka, GPT-4.1-mini redaction, assigns CH_KEY_<GUID>,
#       adds timestamps, archives to MongoDB. Manual commit after write.
# T011: Batches writes to MongoDB (insert_many). Flush on 50 messages or 30s.

import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path

from confluent_kafka import Consumer, KafkaError
from pymongo import MongoClient
from pymongo.errors import PyMongoError

# Load env
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parents[4] / "Code" / ".env")
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

# Version info for stamping records (T008)
VERSION_PATH = Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts" / "content" / "version.json"
ERRORS_PATH = Path(__file__).resolve().parents[4] / "brain" / "machine_artifacts" / "content" / "errors.json"


def _read_version():
    try:
        with open(VERSION_PATH, encoding="utf-8") as f:
            data = json.load(f)
        latest_version = data["versions"]["version"][-1]
        latest_build = latest_version["builds"]["build"][-1]
        return {
            "version": latest_version["version_number"],
            "framework": latest_version["framework_version"],
            "build": latest_build["build_number"],
        }
    except Exception:
        return {"version": "?", "framework": "?", "build": 0}


def _write_error(msg):
    """T007: Write error to errors.json."""
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


def _make_timestamps():
    """B002: PST with 0.1s precision + UTC."""
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + timedelta(hours=-7)
    return (
        now_pst.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now_pst.microsecond // 100000}" + "-07:00",
        now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _redact_content(content, oai_client):
    """B004: GPT-4.1-mini redaction of credentials and profanity."""
    if not content or len(content) < 5:
        return content
    try:
        response = oai_client.chat.completions.create(
            model="gpt-4.1-mini",
            temperature=0,
            messages=[{
                "role": "system",
                "content": (
                    "You are a content redaction engine. Return the SAME text with:\n"
                    "1. API keys, passwords, connection strings, tokens, secrets, or "
                    "credentials replaced with [Sensitive content redacted]\n"
                    "2. Profanity or vulgar language replaced with [expletive redacted]\n"
                    "3. All other text EXACTLY as-is. Do not summarize or rephrase."
                ),
            }, {
                "role": "user",
                "content": content,
            }],
        )
        return response.choices[0].message.content
    except Exception as e:
        _log.warning("Redaction failed: %s — storing unredacted", e)
        return content


def _parse_hook_payload(raw_bytes):
    """Parse the raw payload from the hook. Claude Code sends JSON on stdin."""
    try:
        payload = json.loads(raw_bytes)
        return payload
    except (json.JSONDecodeError, UnicodeDecodeError):
        # Not JSON — store as raw string
        return {"raw": raw_bytes.decode("utf-8", errors="replace")}


def _build_record(payload, headers, version_info, oai_client):
    """Build a MongoDB record from a Kafka message."""
    hook_event = ""
    for h in (headers or []):
        if h[0] == "hook_event":
            hook_event = h[1].decode("utf-8") if isinstance(h[1], bytes) else str(h[1])

    pst, utc = _make_timestamps()

    # Extract content based on hook event type
    prompt = payload.get("prompt", "")
    transcript_path = payload.get("transcript_path", "")

    # Determine actor and content
    actor = "Unknown"
    role = "unknown"
    content = ""

    if prompt:
        # UserPromptSubmit — user typed something
        actor = "Skip"
        role = "user"
        content = prompt
    elif transcript_path:
        # Stop event — extract last assistant message from transcript
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
        # Raw payload — store whatever we got
        content = json.dumps(payload) if isinstance(payload, dict) else str(payload)
        actor = "System"
        role = "system"

    if not content:
        return None

    # B004: Redact
    content = _redact_content(content, oai_client)

    # B002: Build record with unique key and timestamps
    return {
        "ch_key": f"CH_KEY_{uuid.uuid4()}",
        "timestamp_pst": pst,
        "timestamp_utc": utc,
        "actor": actor,
        "role": role,
        "content": content,
        # T008: Version stamps
        "_version": version_info.get("version", "?"),
        "_framework": version_info.get("framework", "?"),
        "_build": version_info.get("build", 0),
    }


def run_consumer():
    """Main consumer loop. Reads from Kafka, batches, writes to MongoDB."""
    _log.info("Starting consumer: bootstrap=%s topic=%s group=%s",
              KAFKA_BOOTSTRAP, KAFKA_TOPIC, KAFKA_GROUP)

    if not MONGO_CONN:
        _log.error("MONGO_FRONTEND_connectionString not set")
        _write_error("Consumer cannot start: MONGO_FRONTEND_connectionString not set")
        sys.exit(1)

    # Initialize OpenAI for redaction (B004)
    try:
        from openai import OpenAI
        oai_client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))
    except Exception as e:
        _log.warning("OpenAI client init failed: %s — redaction disabled", e)
        oai_client = None

    version_info = _read_version()
    _log.info("Version: %s, Framework: %s, Build: %s",
              version_info.get("version"), version_info.get("framework"), version_info.get("build"))

    consumer = Consumer({
        "bootstrap.servers": KAFKA_BOOTSTRAP,
        "group.id": KAFKA_GROUP,
        "auto.offset.reset": "earliest",
        "enable.auto.commit": False,
        "max.poll.interval.ms": 300000,  # 5 min — GPT calls are slow
    })
    consumer.subscribe([KAFKA_TOPIC])
    _log.info("Subscribed to topic: %s", KAFKA_TOPIC)

    # T011: Batch accumulator
    batch = []
    last_flush = time.time()

    try:
        while True:
            msg = consumer.poll(timeout=1.0)

            if msg is None:
                # No message — check if batch needs time-based flush
                if batch and (time.time() - last_flush) >= BATCH_TIMEOUT_SECONDS:
                    _flush_batch(batch, consumer, version_info)
                    batch = []
                    last_flush = time.time()
                continue

            if msg.error():
                if msg.error().code() == KafkaError._PARTITION_EOF:
                    continue
                _log.error("Kafka error: %s", msg.error())
                continue

            # Parse and build record
            try:
                payload = _parse_hook_payload(msg.value())
                record = _build_record(payload, msg.headers(), version_info, oai_client)
                if record:
                    batch.append(record)
                    _log.debug("Batch: %d messages", len(batch))
            except Exception as e:
                _log.error("Failed to process message: %s", e)
                _write_error(f"Consumer process error: {e}")

            # T011: Flush on batch size
            if len(batch) >= BATCH_SIZE:
                _flush_batch(batch, consumer, version_info)
                batch = []
                last_flush = time.time()

    except KeyboardInterrupt:
        _log.info("Consumer shutting down")
    finally:
        # Flush remaining batch
        if batch:
            _flush_batch(batch, consumer, version_info)
        consumer.close()
        _log.info("Consumer closed")


def _flush_batch(batch, consumer, version_info):
    """T011: Write batch to MongoDB using insert_many. Commit offset after success."""
    if not batch:
        return

    _log.info("Flushing %d records to MongoDB", len(batch))

    try:
        client = MongoClient(MONGO_CONN, serverSelectionTimeoutMS=10000)
        db = client[MONGO_DB]
        coll = db[MONGO_COLLECTION]

        # Deduplicate by ch_key (at-least-once may produce duplicates)
        existing_keys = set()
        for doc in coll.find(
            {"ch_key": {"$in": [r["ch_key"] for r in batch]}},
            {"ch_key": 1}
        ):
            existing_keys.add(doc["ch_key"])

        new_records = [r for r in batch if r["ch_key"] not in existing_keys]

        if new_records:
            coll.insert_many(new_records, ordered=False)
            _log.info("Archived %d records (%d duplicates skipped)",
                      len(new_records), len(batch) - len(new_records))
        else:
            _log.info("All %d records already in MongoDB (duplicates)", len(batch))

        client.close()

        # T003: Manual commit after successful write
        consumer.commit(asynchronous=False)
        _log.info("Committed offset")

    except PyMongoError as e:
        _log.error("MongoDB write failed: %s — will retry on next flush", e)
        _write_error(f"MongoDB batch write failed: {e}")
        # Do NOT commit — Kafka will redeliver on next poll


if __name__ == "__main__":
    run_consumer()
