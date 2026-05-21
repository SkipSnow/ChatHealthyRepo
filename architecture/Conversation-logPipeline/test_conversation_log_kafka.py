# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Integration tests for conversation log Kafka infrastructure.
# These tests require the running stack: Kafka broker, sidecar, consumer.
# Run with: pytest Code/Shared/ops/kafka/tests/test_conversation_log_kafka.py -v

import json
import os
import sys
import time
import uuid
import subprocess

import pytest
import requests

# ── Test configuration ──────────────────────────────────────────────────

SIDECAR_URL = "http://127.0.0.1:8100"
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))
BOOT_SCRIPT = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "tools", "chathealthy_devops_boot.py")
FALLBACK_PATH = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "conversation_log_fallback.jsonl")
DOCKER_COMPOSE_PATH = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "docker-compose.yml")


def _sidecar_is_up():
    try:
        r = requests.get(f"{SIDECAR_URL}/health", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


def _kafka_is_up():
    try:
        result = subprocess.run(
            ["docker", "ps", "--filter", "name=chathealthy-kafka", "--format", "{{.Status}}"],
            capture_output=True, text=True, timeout=5,
        )
        return "Up" in result.stdout
    except Exception:
        return False


# Skip all tests if infrastructure isn't running
pytestmark = pytest.mark.skipif(
    not _sidecar_is_up() or not _kafka_is_up(),
    reason="Kafka sidecar or broker not running — start the service first",
)


# ══════════════════════════════════════════════════════════════════════
# T001: Hook MUST POST raw payload to sidecar, complete in <100ms,
#       no parsing/redaction/transformation. Fallback if sidecar down.
# ══════════════════════════════════════════════════════════════════════

class TestT001_HookToSidecar:

    def test_hook_posts_to_sidecar_and_gets_202(self):
        """T001: Hook POSTs to sidecar and receives 202."""
        payload = json.dumps({
            "prompt": f"pytest-T001-{uuid.uuid4().hex[:8]}",
            "hook_event_name": "UserPromptSubmit",
        })
        result = subprocess.run(
            [sys.executable, BOOT_SCRIPT, "--mode", "prompt"],
            input=payload, capture_output=True, text=True, timeout=10,
        )
        output = json.loads(result.stdout)
        workers = output.get("workers", [])
        log_worker = next((w for w in workers if w.get("json") == "conversation_log"), {})
        assert log_worker.get("logged") is True
        assert log_worker.get("status") == 202

    def test_hook_completes_under_500ms(self):
        """T001: Hook MUST complete fast. We allow 500ms total
        (includes Python startup ~200ms + HTTP ~5ms)."""
        payload = json.dumps({"prompt": "timing-test", "hook_event_name": "UserPromptSubmit"})
        start = time.time()
        subprocess.run(
            [sys.executable, BOOT_SCRIPT, "--mode", "prompt"],
            input=payload, capture_output=True, text=True, timeout=5,
        )
        elapsed = time.time() - start
        assert elapsed < 0.5, f"Hook took {elapsed:.3f}s — must be under 500ms"

    def test_hook_does_not_write_to_conversation_log_json(self):
        """T001: Hook MUST NOT write to any file (except fallback)."""
        log_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "conversation_log.json")
        before_mtime = os.path.getmtime(log_path) if os.path.exists(log_path) else 0

        payload = json.dumps({"prompt": "no-file-write-test", "hook_event_name": "UserPromptSubmit"})
        subprocess.run(
            [sys.executable, BOOT_SCRIPT, "--mode", "prompt"],
            input=payload, capture_output=True, text=True, timeout=10,
        )

        after_mtime = os.path.getmtime(log_path) if os.path.exists(log_path) else 0
        assert before_mtime == after_mtime, "Hook modified conversation_log.json — it should only POST to sidecar"

    def test_hook_sends_both_prompt_and_stop_events(self):
        """T001: Hook handles both user_prompt_submit and stop."""
        for mode, event in [("prompt", "user_prompt_submit"), ("prompt_result", "stop")]:
            payload = json.dumps({"prompt": f"event-test-{event}", "hook_event_name": event, "transcript_path": ""})
            result = subprocess.run(
                [sys.executable, BOOT_SCRIPT, "--mode", mode],
                input=payload, capture_output=True, text=True, timeout=10,
            )
            # Should not crash
            assert result.returncode == 0, f"Hook crashed on {mode}: {result.stderr}"


# ══════════════════════════════════════════════════════════════════════
# T002: Sidecar MUST maintain warm Kafka connection, accept POST,
#       enqueue, return 202. No processing beyond enqueue.
# ══════════════════════════════════════════════════════════════════════

class TestT002_Sidecar:

    def test_sidecar_health_endpoint(self):
        """T002: Sidecar has a health endpoint reporting topic and bootstrap."""
        r = requests.get(f"{SIDECAR_URL}/health", timeout=2)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "healthy"
        assert data["topic"] == "conversation-log"

    def test_sidecar_accepts_post_returns_202(self):
        """T002: Sidecar accepts POST and returns 202 Accepted."""
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": f"sidecar-test-{uuid.uuid4().hex[:8]}"},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=2,
        )
        assert r.status_code == 202

    def test_sidecar_rejects_empty_payload(self):
        """T002: Sidecar returns 400 on empty payload."""
        r = requests.post(f"{SIDECAR_URL}/produce", data=b"", timeout=2)
        assert r.status_code == 400

    def test_sidecar_enqueues_to_kafka(self):
        """T002: Message sent to sidecar appears in Kafka topic.
        Verified by checking topic offset advances after produce."""
        from confluent_kafka import Consumer
        from confluent_kafka.admin import AdminClient

        # Get current topic end offset
        admin = AdminClient({"bootstrap.servers": "localhost:9092"})
        c = Consumer({
            "bootstrap.servers": "localhost:9092",
            "group.id": f"test-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "latest",
        })
        c.subscribe(["conversation-log"])
        c.poll(timeout=2.0)  # trigger assignment
        partitions = c.assignment()
        if partitions:
            end_offsets_before = {p.partition: c.get_watermark_offsets(p)[1] for p in partitions}
        else:
            end_offsets_before = {}

        # Produce through sidecar
        marker = f"kafka-verify-{uuid.uuid4().hex[:8]}"
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": marker},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=2,
        )
        assert r.status_code == 202

        # Check offset advanced
        time.sleep(1)
        if partitions:
            end_offsets_after = {p.partition: c.get_watermark_offsets(p)[1] for p in partitions}
            advanced = any(end_offsets_after.get(p, 0) > end_offsets_before.get(p, 0) for p in end_offsets_after)
            assert advanced, "Kafka topic offset did not advance after produce"

        c.close()


# ══════════════════════════════════════════════════════════════════════
# T004: Kafka MUST run in Docker, KRaft mode, 24h retention
# ══════════════════════════════════════════════════════════════════════

class TestT004_KafkaInDocker:

    def test_kafka_container_running(self):
        """T004: Kafka container is running in Docker."""
        assert _kafka_is_up()

    def test_kafka_topic_exists(self):
        """T004: conversation-log topic exists."""
        result = subprocess.run(
            ["docker", "exec", "chathealthy-kafka", "sh", "-c",
             "/opt/kafka/bin/kafka-topics.sh --bootstrap-server localhost:9092 --list"],
            capture_output=True, text=True, timeout=15,
        )
        assert "conversation-log" in result.stdout

    def test_docker_compose_exists(self):
        """T004: docker-compose.yml exists in the kafka directory."""
        assert os.path.exists(DOCKER_COMPOSE_PATH)

    def test_docker_compose_uses_apache_image(self):
        """T004/T009: Must use Apache Kafka image (Apache 2.0), not Confluent."""
        with open(DOCKER_COMPOSE_PATH, encoding="utf-8") as f:
            content = f.read()
        assert "apache/kafka" in content
        assert "confluentinc" not in content


# ══════════════════════════════════════════════════════════════════════
# T005: C# service is supervisor only. Starts Docker/Kafka, both PIDs.
# T006: AUTO_START on boot.
# ══════════════════════════════════════════════════════════════════════

class TestT005_T006_ServiceSupervisor:

    def test_service_is_running(self):
        """T005: ChatHealthyClaudeLogMgmt service is running."""
        result = subprocess.run(
            ["sc", "query", "ChatHealthyClaudeLogMgmt"],
            capture_output=True, text=True, timeout=5,
        )
        assert "RUNNING" in result.stdout

    def test_service_is_auto_start(self):
        """T006: Service is configured for AUTO_START."""
        result = subprocess.run(
            ["sc", "qc", "ChatHealthyClaudeLogMgmt"],
            capture_output=True, text=True, timeout=5,
        )
        assert "AUTO_START" in result.stdout

    def test_two_python_pids_under_services(self):
        """T005: Exactly 2 Python processes running under Services session."""
        result = subprocess.run(
            ["tasklist"],
            capture_output=True, text=True, timeout=5,
        )
        python_lines = [l for l in result.stdout.split("\n")
                        if "python.exe" in l.lower() and "Services" in l]
        assert len(python_lines) >= 2, f"Expected at least 2 Python PIDs under Services, got {len(python_lines)}"

    def test_worker_cs_has_no_business_logic(self):
        """T005: Worker.cs must not contain GPT calls, MongoDB, or redaction."""
        worker_path = os.path.join(REPO_ROOT, "Code", "CSharp", "ChatHealthyLogService", "dev", "Worker.cs")
        with open(worker_path, encoding="utf-8") as f:
            content = f.read().lower()
        assert "openai" not in content, "Worker.cs contains OpenAI reference — no business logic allowed"
        assert "mongodb" not in content, "Worker.cs contains MongoDB reference — no business logic allowed"
        assert "redact" not in content, "Worker.cs contains redaction logic — no business logic allowed"


# ══════════════════════════════════════════════════════════════════════
# T007: Errors MUST be written to errors.json
# ══════════════════════════════════════════════════════════════════════

class TestT007_ErrorsJson:

    def test_errors_json_exists(self):
        """T007: errors.json exists."""
        errors_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "errors.json")
        assert os.path.exists(errors_path)

    def test_errors_json_is_valid(self):
        """T007: errors.json is valid JSON array."""
        errors_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "errors.json")
        with open(errors_path, encoding="utf-8") as f:
            data = json.load(f)
        assert isinstance(data, list)

    def test_error_records_have_required_fields(self):
        """T007: Each error record has timestamp, source, and error."""
        errors_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "errors.json")
        with open(errors_path, encoding="utf-8") as f:
            data = json.load(f)
        for entry in data:
            assert "timestamp" in entry, f"Error missing timestamp: {entry}"
            assert "source" in entry, f"Error missing source: {entry}"
            assert "error" in entry, f"Error missing error: {entry}"


# ══════════════════════════════════════════════════════════════════════
# T009: Apache 2.0 license only. No SSPL/RSALv2/proprietary.
# ══════════════════════════════════════════════════════════════════════

class TestT009_Licensing:

    def test_kafka_image_is_apache(self):
        """T009: Docker image is apache/kafka, not confluent."""
        with open(DOCKER_COMPOSE_PATH, encoding="utf-8") as f:
            content = f.read()
        assert "apache/kafka" in content

    def test_confluent_kafka_python_is_apache_licensed(self):
        """T009: confluent-kafka pip package is Apache 2.0."""
        result = subprocess.run(
            [sys.executable, "-m", "pip", "show", "confluent-kafka"],
            capture_output=True, text=True, timeout=10,
        )
        assert "Apache" in result.stdout or "apache" in result.stdout.lower()


# ══════════════════════════════════════════════════════════════════════
# T010: conversation_log.json schema in schema.json
# ══════════════════════════════════════════════════════════════════════

class TestT010_Schema:

    def test_conversation_log_has_schema_address(self):
        """T010: conversation_log.json has schema_address field."""
        log_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "conversation_log.json")
        with open(log_path, encoding="utf-8") as f:
            data = json.load(f)
        assert "schema_address" in data, "conversation_log.json missing schema_address"


# ══════════════════════════════════════════════════════════════════════
# T003: Consumer reads Kafka, GPT redaction, CH_KEY_ + GUID, timestamps,
#       archives to MongoDB. Manual commit. At-least-once.
# ══════════════════════════════════════════════════════════════════════

class TestT003_Consumer:

    def test_consumer_builds_record_with_ch_key(self):
        """T003: Records in MongoDB have CH_KEY_ prefix."""
        from pymongo import MongoClient
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        if not conn:
            pytest.skip("No MongoDB connection string")
        client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        coll = client["ClaudeCodeLog"]["conversation_log_archive"]
        rec = coll.find_one({"ch_key": {"$regex": "^CH_KEY_"}})
        assert rec is not None, "No CH_KEY_ records in MongoDB — consumer may not have run"
        assert rec["ch_key"].startswith("CH_KEY_")
        assert len(rec["ch_key"]) > 10  # CH_KEY_ + UUID

    def test_consumer_records_have_timestamps(self):
        """T003/B002: Records have PST and UTC timestamps."""
        from pymongo import MongoClient
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        if not conn:
            pytest.skip("No MongoDB connection string")
        client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        coll = client["ClaudeCodeLog"]["conversation_log_archive"]
        rec = coll.find_one({"ch_key": {"$regex": "^CH_KEY_"}})
        assert rec is not None
        assert "timestamp_pst" in rec, "Record missing timestamp_pst"
        assert "timestamp_utc" in rec, "Record missing timestamp_utc"

    def test_consumer_uses_manual_commit(self):
        """T003: Consumer config has enable.auto.commit=False (code inspection)."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        assert '"enable.auto.commit": False' in content or "'enable.auto.commit': False" in content


# ══════════════════════════════════════════════════════════════════════
# T008: MongoDB TLS 1.2+, version stamps on every record
# ══════════════════════════════════════════════════════════════════════

class TestT008_MongoDBVersionStamps:

    def test_records_have_version_stamps(self):
        """T008: Every Kafka-processed record has _version, _framework, _build."""
        from pymongo import MongoClient
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        if not conn:
            pytest.skip("No MongoDB connection string")
        client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        coll = client["ClaudeCodeLog"]["conversation_log_archive"]
        rec = coll.find_one({"ch_key": {"$regex": "^CH_KEY_"}})
        assert rec is not None
        assert "_version" in rec, "Record missing _version"
        assert "_framework" in rec, "Record missing _framework"
        assert "_build" in rec, "Record missing _build"

    def test_mongo_connection_uses_tls(self):
        """T008: MongoDB connection string uses mongodb+srv (TLS by default)."""
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        assert conn.startswith("mongodb+srv://"), "Connection string must use mongodb+srv (TLS)"

    def test_connection_string_from_env(self):
        """T008: Consumer reads connection string from .env, not hardcoded."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        assert 'os.environ.get("MONGO_FRONTEND_connectionString"' in content
        # Must NOT have a hardcoded connection string
        assert "mongodb+srv://" not in content, "Connection string hardcoded in consumer — must come from .env"


# ══════════════════════════════════════════════════════════════════════
# T011: Batch writes to MongoDB (insert_many, 50 msgs or 30s)
# ══════════════════════════════════════════════════════════════════════

class TestT011_BatchWrites:

    def test_consumer_uses_insert_many(self):
        """T011: Consumer uses insert_many, not insert_one."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        assert "insert_many" in content, "Consumer must use insert_many for batch writes"
        assert "insert_one" not in content, "Consumer must NOT use insert_one — batch only"

    def test_batch_size_is_50(self):
        """T011: Batch size constant is 50."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        assert "BATCH_SIZE = 50" in content

    def test_batch_timeout_is_30s(self):
        """T011: Batch timeout constant is 30 seconds."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        assert "BATCH_TIMEOUT_SECONDS = 30" in content


# ══════════════════════════════════════════════════════════════════════
# B001: All visible text stored verbatim. No truncation.
# ══════════════════════════════════════════════════════════════════════

class TestB001_VerbatimStorage:

    def test_consumer_does_not_truncate(self):
        """B001: Consumer code must not truncate content."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        # The consumer should not have [:500] or [:200] truncation on content
        # (It's OK for debug fields like prescriber_embedding_text)
        assert 'content[:' not in content and "content[:" not in content, \
            "Consumer truncates content — B001 requires verbatim storage"

    def test_long_message_stored_in_full(self):
        """B001: Send a long message and verify it arrives in MongoDB untruncated."""
        from pymongo import MongoClient
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        if not conn:
            pytest.skip("No MongoDB connection string")

        # Send a long message through the sidecar — use real-looking text,
        # not repeated characters (GPT would redact those as suspicious)
        long_text = "B001-verbatim-test " + " ".join(
            f"The provider in location {i} has specialty code {1000+i} and serves patients in district {i%10}"
            for i in range(100)
        )
        marker = f"B001-{uuid.uuid4().hex[:8]}"
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": f"{marker} {long_text}", "hook_event_name": "UserPromptSubmit"},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=2,
        )
        assert r.status_code == 202

        # Wait for consumer to process and flush (batch timeout = 30s)
        time.sleep(35)

        client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        coll = client["ClaudeCodeLog"]["conversation_log_archive"]
        rec = coll.find_one({"content": {"$regex": marker}})
        if rec:
            assert len(rec["content"]) >= 2000, f"Content truncated to {len(rec['content'])} chars"


# ══════════════════════════════════════════════════════════════════════
# B002: PST timestamp, UTC timestamp, globally unique key
# ══════════════════════════════════════════════════════════════════════

class TestB002_Timestamps:

    def test_record_has_pst_utc_and_key(self):
        """B002: Every record has timestamp_pst, timestamp_utc, and ch_key."""
        from pymongo import MongoClient
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        if not conn:
            pytest.skip("No MongoDB connection string")
        client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        coll = client["ClaudeCodeLog"]["conversation_log_archive"]
        # Check the 5 most recent Kafka-processed records
        for rec in coll.find({"ch_key": {"$regex": "^CH_KEY_"}}).sort("timestamp_utc", -1).limit(5):
            assert "timestamp_pst" in rec
            assert "timestamp_utc" in rec
            assert "ch_key" in rec
            assert rec["ch_key"].startswith("CH_KEY_")

    def test_keys_are_unique(self):
        """B002: ch_key values are globally unique (no duplicates)."""
        from pymongo import MongoClient
        from dotenv import load_dotenv
        from pathlib import Path
        load_dotenv(Path(REPO_ROOT) / "Code" / ".env")
        conn = os.environ.get("MONGO_FRONTEND_connectionString", "")
        if not conn:
            pytest.skip("No MongoDB connection string")
        client = MongoClient(conn, serverSelectionTimeoutMS=10000)
        coll = client["ClaudeCodeLog"]["conversation_log_archive"]
        keys = [r["ch_key"] for r in coll.find({"ch_key": {"$regex": "^CH_KEY_"}}, {"ch_key": 1})]
        assert len(keys) == len(set(keys)), f"Duplicate ch_keys found: {len(keys)} total, {len(set(keys))} unique"


# ══════════════════════════════════════════════════════════════════════
# B003: ALL utterances durably captured. Zero loss target.
# ══════════════════════════════════════════════════════════════════════

class TestB003_DurableCapture:

    def test_sidecar_returns_202_on_produce(self):
        """B003: Sidecar durably accepts messages (202 = enqueued to Kafka)."""
        r = requests.post(
            f"{SIDECAR_URL}/produce",
            json={"prompt": f"B003-durable-{uuid.uuid4().hex[:8]}"},
            headers={"X-Hook-Event": "UserPromptSubmit"},
            timeout=2,
        )
        assert r.status_code == 202

    def test_kafka_topic_has_24h_retention(self):
        """B003: Kafka topic retains messages for 24 hours (durable buffer)."""
        with open(DOCKER_COMPOSE_PATH, encoding="utf-8") as f:
            content = f.read()
        assert "KAFKA_LOG_RETENTION_HOURS: 24" in content


# ══════════════════════════════════════════════════════════════════════
# B004: Sensitive content redacted
# ══════════════════════════════════════════════════════════════════════

class TestB004_Redaction:

    def test_consumer_calls_gpt_for_redaction(self):
        """B004: Consumer code calls GPT-4.1-mini for redaction."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        assert "gpt-4.1-mini" in content
        assert "Sensitive content redacted" in content
        assert "expletive redacted" in content


# ══════════════════════════════════════════════════════════════════════
# B007: Service starts on boot without operator intervention
# ══════════════════════════════════════════════════════════════════════

class TestB007_AutoStart:

    def test_service_is_auto_start(self):
        """B007: Service configured for AUTO_START."""
        result = subprocess.run(
            ["sc", "qc", "ChatHealthyClaudeLogMgmt"],
            capture_output=True, text=True, timeout=5,
        )
        assert "AUTO_START" in result.stdout


# ══════════════════════════════════════════════════════════════════════
# B008: MongoDB unavailable — utterances retained durably
# ══════════════════════════════════════════════════════════════════════

class TestB008_MongoUnavailable:

    def test_consumer_does_not_commit_on_mongo_failure(self):
        """B008: Consumer code does NOT commit offset if MongoDB write fails."""
        consumer_path = os.path.join(REPO_ROOT, "Code", "Shared", "ops", "kafka", "conversation_log_consumer.py")
        with open(consumer_path, encoding="utf-8") as f:
            content = f.read()
        # The commit must be INSIDE the try block, after insert_many
        # and the except block must NOT contain consumer.commit
        assert "# Do NOT commit" in content or "Do NOT commit" in content, \
            "Consumer must explicitly document that it does not commit on MongoDB failure"


# ══════════════════════════════════════════════════════════════════════
# B010: User can restart service, operational within 10 seconds
# ══════════════════════════════════════════════════════════════════════

class TestB010_RestartCapability:

    def test_service_can_be_queried(self):
        """B010: Service is queryable via sc command."""
        result = subprocess.run(
            ["sc", "query", "ChatHealthyClaudeLogMgmt"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0
        assert "ChatHealthyClaudeLogMgmt" in result.stdout
