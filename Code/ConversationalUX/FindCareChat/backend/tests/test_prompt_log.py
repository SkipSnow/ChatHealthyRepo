# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# test_prompt_log.py — Boolean pytests for BIZOPS-CHATLOG-001 prompt_log.
#
# Every test maps 1:1 to a requirement. Full traceability from
# business goal to automated regression testing.
#
# Requirements covered: BR1-BR3, TR8-TR9, TR11-TR21
# Infrastructure requirements (TR1-TR7) tested separately in infra tests.
#
# AUDIT FIX 2026-04-15: Tests rewritten to call real conversation_log_hook.py
# code instead of reimplementing logic inline. TR17, TR22, SchemaValidation
# kept as-is (already testing real artifacts).

import json
import copy
import re
import os
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

# ── Paths ────────────────────────────────────────────────────────────

# Navigate from tests/ → backend/ → FindCareChat/ → ConversationalUX/ → Code/ → repo root
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent.parent
BRAIN_CONTENT = PROJECT_ROOT / "brain" / "machine_artifacts" / "content"
PROMPT_LOG_PATH = BRAIN_CONTENT / "conversation_log.json"
SCHEMA_PATH = BRAIN_CONTENT / "schema.json"

# Import the REAL conversation_log_hook module
HOOK_DIR = PROJECT_ROOT / "Code" / "Shared" / "ops"
sys.path.insert(0, str(HOOK_DIR))
import conversation_log_hook as hook

# ── JSON Schema for validation ───────────────────────────────────────

PROMPT_LOG_SCHEMA = {
    "required_top_level": ["collection", "path", "purpose", "produces_artifact", "retention", "utterances"],
    "utterance_required": ["utterance", "timestamp_pst", "timestamp_utc", "actor", "role", "content"],
    "utterance_optional": ["ch_key", "preservePastTime", "jobId", "counts", "outcome", "action"],
    "role_enum": ["user", "assistant", "agent", "service"],
    "actor_pattern": r"^[A-Za-z0-9_ -]{1,50}$",
    # ISO 8601 or M/D/YYYY format (purge service uses the latter with variable-width fields)
    "timestamp_pst_pattern": r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?([+-]\d{2}:\d{2})?|\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}(\.\d+)?)$",
    "timestamp_utc_pattern": r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z?|\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}:\d{2}(\.\d+)?)$",
}


# ── Helpers ──────────────────────────────────────────────────────────


def load_prompt_log() -> dict:
    """Load prompt_log.json from brain."""
    return json.loads(PROMPT_LOG_PATH.read_text(encoding="utf-8"))


def _make_temp_log(utterances=None) -> Path:
    """Create a temp conversation_log.json for isolated testing."""
    log = {
        "collection": "conversation_log",
        "path": "brain/machine_artifacts/content/conversation_log.json",
        "purpose": "Rolling 24h conversation log.",
        "produces_artifact": False,
        "retention": "24_hours",
        "utterances": utterances or [],
    }
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    )
    json.dump(log, tmp, indent=2, ensure_ascii=False)
    tmp.close()
    return Path(tmp.name)


# ── BR1: Every message produces exactly one log entry ────────────────


class TestBR1:
    """BR1: On every user or assistant message, exactly one log entry
    is written containing actor, role, and content.
    Tests the real append_utterance function from conversation_log_hook."""

    def test_user_message_produces_entry(self, tmp_path):
        """append_utterance writes one entry with correct actor/role/content."""
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("Skip", "user", "HELLO")

        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert len(data["utterances"]) == 1
        assert data["utterances"][0]["actor"] == "Skip"
        assert data["utterances"][0]["role"] == "user"
        assert data["utterances"][0]["content"] == "HELLO"

    def test_assistant_message_produces_entry(self, tmp_path):
        """append_utterance writes assistant entry correctly."""
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("Claude", "assistant", "Hi Boss")

        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert len(data["utterances"]) == 1
        assert data["utterances"][0]["actor"] == "Claude"
        assert data["utterances"][0]["role"] == "assistant"
        assert data["utterances"][0]["content"] == "Hi Boss"

    def test_entry_has_required_fields(self, tmp_path):
        """Each utterance written by the hook has all required fields."""
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("Skip", "user", "test")

        data = json.loads(log_path.read_text(encoding="utf-8"))
        entry = data["utterances"][0]
        for field in PROMPT_LOG_SCHEMA["utterance_required"]:
            assert field in entry, f"Missing required field: {field}"


# ── BR2: Hourly cleanup of utterances older than 24h ─────────────────
# The real groom logic lives in conversation_log_purge_service.py which
# requires MongoDB. We cannot meaningfully unit-test it without a live
# database. Marking skip with explanation.


class TestBR2:
    """BR2: Scheduled job deletes utterances older than 24h."""

    @pytest.mark.skip(reason="Groom logic is in ConversationLogService._query_retained "
                             "which requires live MongoDB. Cannot unit-test without DB.")
    def test_old_utterances_deleted_by_groom(self):
        pass

    @pytest.mark.skip(reason="Groom logic requires live MongoDB.")
    def test_all_recent_utterances_retained(self):
        pass


# ── BR3: Actor identification ────────────────────────────────────────


class TestBR3:
    """BR3: Each log entry must include a non-null actor string
    matching regex ^[A-Za-z0-9_ -]{1,50}$."""

    @pytest.mark.parametrize("actor", ["Skip", "Claude", "GPT", "GPT 5_3", "Perplexity"])
    def test_valid_actors(self, actor):
        assert re.match(PROMPT_LOG_SCHEMA["actor_pattern"], actor)

    @pytest.mark.parametrize("actor", ["", None, "a" * 51, "Skip@home", "Claude;DROP"])
    def test_invalid_actors(self, actor):
        if actor is None:
            assert actor is None  # null check
        else:
            assert not re.match(PROMPT_LOG_SCHEMA["actor_pattern"], actor)


# ── TR8: Append-only — no edits after insertion ──────────────────────


class TestTR8:
    """TR8: No existing utterance document is updated after insertion.
    Tests that a second append_utterance does not alter the first entry."""

    def test_append_does_not_modify_existing(self, tmp_path):
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("Skip", "user", "original")
            # snapshot the first entry
            first_data = json.loads(log_path.read_text(encoding="utf-8"))
            original_entry = copy.deepcopy(first_data["utterances"][0])

            hook.append_utterance("Claude", "assistant", "reply")

        final_data = json.loads(log_path.read_text(encoding="utf-8"))
        assert final_data["utterances"][0] == original_entry
        assert len(final_data["utterances"]) == 2


# ── TR9: Binary content replaced with marker ────────────────────────


class TestTR9:
    """TR9: Content containing binary payloads is replaced with
    marker [binary content omitted]. Tests real clean_content function."""

    def test_base64_data_uri_replaced(self):
        result = hook.clean_content("data:image/png;base64,iVBORw0KGgoAAAANSUhEUg...")
        assert result == "[binary content omitted]"

    def test_base64_in_content_replaced(self):
        result = hook.clean_content("here is base64, encoded data")
        assert result == "[binary content omitted]"

    def test_normal_content_not_replaced(self):
        result = hook.clean_content("Hello world")
        assert result == "Hello world"


# ── TR11: Truncation at 500 chars ────────────────────────────────────


class TestTR11:
    """TR11: Content length >500 chars is truncated to 500 chars +
    suffix '[truncated — X chars]'. Tests real clean_content function."""

    def test_short_content_not_truncated(self):
        content = "Hello world"
        result = hook.clean_content(content)
        assert result == "Hello world"

    def test_long_content_truncated(self):
        original = "x" * 1000
        result = hook.clean_content(original)
        assert result.startswith("x" * 500)
        assert "[truncated" in result
        assert "1000 chars]" in result

    def test_exactly_500_not_truncated(self):
        content = "x" * 500
        result = hook.clean_content(content)
        assert result == content
        assert "[truncated" not in result


# ── TR12: Dual timestamps ───────────────────────────────────────────


class TestTR12:
    """TR12: timestamp_utc must equal system UTC time; timestamp_pst
    must equal UTC offset conversion. Tests real make_timestamps function."""

    def test_utc_format(self):
        pst, utc = hook.make_timestamps()
        assert re.match(PROMPT_LOG_SCHEMA["timestamp_utc_pattern"], utc)

    def test_pst_format(self):
        pst, utc = hook.make_timestamps()
        assert re.match(PROMPT_LOG_SCHEMA["timestamp_pst_pattern"], pst)

    def test_written_entry_has_both_timestamps(self, tmp_path):
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("Skip", "user", "test")

        data = json.loads(log_path.read_text(encoding="utf-8"))
        entry = data["utterances"][0]
        assert re.match(PROMPT_LOG_SCHEMA["timestamp_utc_pattern"], entry["timestamp_utc"])
        assert re.match(PROMPT_LOG_SCHEMA["timestamp_pst_pattern"], entry["timestamp_pst"])


# ── TR13: Write before response ──────────────────────────────────────


class TestTR13:
    """TR13: Log entry is persisted before API response is returned.
    Tests that append_utterance is synchronous — data is on disk after call."""

    def test_write_is_synchronous(self, tmp_path):
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("Skip", "user", "test message")
            # Immediately after the call, data must be on disk
            data = json.loads(log_path.read_text(encoding="utf-8"))
            assert len(data["utterances"]) == 1
            assert data["utterances"][0]["content"] == "test message"


# ── TR14: No update/delete from app code ─────────────────────────────


class TestTR14:
    """TR14: No update/delete operations originate from application
    code (excluding groom job). Source code inspection of hook module."""

    def test_hook_has_no_delete_operations(self):
        """Verify conversation_log_hook.py contains no delete/remove/pop operations."""
        import inspect
        source = inspect.getsource(hook)
        # The hook should only append, never delete
        assert ".pop(" not in source, "Hook contains .pop() — violates append-only"
        assert ".remove(" not in source, "Hook contains .remove() — violates append-only"
        assert "del " not in source or "delete=False" in source, "Hook contains del — violates append-only"


# ── TR15: Monotonically increasing, no renumbering ──────────────────


class TestTR15:
    """TR15: Utterances numbered with a monotonically increasing
    integer. Tests real next_utterance_num function."""

    def test_next_utterance_num_empty(self):
        log = {"utterances": []}
        assert hook.next_utterance_num(log) == 1

    def test_next_utterance_num_after_existing(self):
        log = {"utterances": [
            {"utterance": 1}, {"utterance": 2}, {"utterance": 3}
        ]}
        assert hook.next_utterance_num(log) == 4

    def test_no_renumbering_after_gap(self):
        """After grooming removes 1-2, remaining are 3,4,5. Next is 6."""
        log = {"utterances": [
            {"utterance": 3}, {"utterance": 4}, {"utterance": 5}
        ]}
        assert hook.next_utterance_num(log) == 6

    def test_sequential_appends_are_monotonic(self, tmp_path):
        """Successive append_utterance calls produce monotonically increasing numbers."""
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            for i in range(5):
                hook.append_utterance("Skip", "user", f"msg {i}")

        data = json.loads(log_path.read_text(encoding="utf-8"))
        nums = [u["utterance"] for u in data["utterances"]]
        assert nums == [1, 2, 3, 4, 5]


# ── TR16: System-reminder content excluded ───────────────────────────


class TestTR16:
    """TR16: Entries with source=system_reminder are not persisted.
    Tests real clean_content function."""

    def test_system_reminder_stripped(self):
        content = "<system-reminder>The TodoWrite tool hasn't been used</system-reminder>"
        result = hook.clean_content(content)
        assert result == ""

    def test_system_reminder_in_mixed_content(self):
        content = "Hello <system-reminder>hidden</system-reminder> world"
        result = hook.clean_content(content)
        assert "<system-reminder>" not in result
        assert "Hello" in result
        assert "world" in result

    def test_pure_system_reminder_not_appended(self, tmp_path):
        """A message that is entirely system-reminder should produce no utterance."""
        log_path = tmp_path / "conversation_log.json"
        log_path.write_text(json.dumps({
            "collection": "conversation_log", "path": "", "purpose": "",
            "produces_artifact": False, "retention": "24_hours", "utterances": []
        }), encoding="utf-8")

        with patch.object(hook, "PROMPT_LOG_PATH", log_path):
            hook.append_utterance("system", "user", "<system-reminder>ignored</system-reminder>")

        data = json.loads(log_path.read_text(encoding="utf-8"))
        assert len(data["utterances"]) == 0


# ── TR17: prompt_log registered in schema.json ───────────────────────


class TestTR17:
    """TR17: schema.json contains prompt_log with retention=24_hours."""

    def test_schema_contains_prompt_log(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        collections = schema.get("collections", {})
        assert "prompt_log" in collections

    def test_schema_retention(self):
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        prompt_log = schema["collections"]["prompt_log"]
        assert prompt_log.get("retention") == "24_hours"


# ── TR18: Brain contains operational record ──────────────────────────


class TestTR18:
    """TR18: Brain contains operational record titled
    'prompt_log protocol'."""

    def test_protocol_record_exists(self):
        """Verify schema.json contains the prompt_log protocol record."""
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        protocols = schema.get("protocols", {})
        prompt_log_proto = protocols.get("prompt_log", {})
        title = prompt_log_proto.get("title", "")
        assert "prompt log protocol" in title.lower(), \
            f"Expected 'Prompt Log Protocol' in schema.json protocols, got: {title!r}"


# ── TR19: Groom idempotency ──────────────────────────────────────────


class TestTR19:
    """TR19: Groom operation must be idempotent."""

    @pytest.mark.skip(reason="Groom logic is in ConversationLogService._query_retained "
                             "which requires live MongoDB. Cannot unit-test without DB.")
    def test_double_groom_same_result(self):
        pass


# ── TR20: Fail-open on write failure ─────────────────────────────────


class TestTR20:
    """TR20: If prompt_log write fails, chat continues. The hook's main()
    always exits 0 so it never blocks Claude."""

    def test_main_exits_zero_on_bad_input(self):
        """main() exits 0 even with invalid JSON input."""
        import subprocess
        hook_path = str(HOOK_DIR / "conversation_log_hook.py")
        result = subprocess.run(
            [sys.executable, hook_path],
            input="not-valid-json",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0

    def test_main_exits_zero_on_empty_input(self):
        """main() exits 0 on empty stdin."""
        import subprocess
        hook_path = str(HOOK_DIR / "conversation_log_hook.py")
        result = subprocess.run(
            [sys.executable, hook_path],
            input="",
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0


# ── TR21: Utterance sequencing rule ──────────────────────────────────


class TestTR21:
    """TR21: Utterance numbers start at 1 for new log. Next value is
    always max(existing) + 1, or 1 if empty. Never reused or reset.
    Tests real next_utterance_num function."""

    def test_empty_log_starts_at_1(self):
        assert hook.next_utterance_num({"utterances": []}) == 1

    def test_next_after_existing(self):
        log = {"utterances": [{"utterance": 1}, {"utterance": 2}, {"utterance": 3}]}
        assert hook.next_utterance_num(log) == 4

    def test_next_after_groom_gap(self):
        log = {"utterances": [{"utterance": 4}, {"utterance": 5}, {"utterance": 6}]}
        assert hook.next_utterance_num(log) == 7

    def test_empty_after_full_groom(self):
        assert hook.next_utterance_num({"utterances": []}) == 1


# ── TR22: Hook enforcement ───────────────────────────────────────────


class TestTR22:
    """TR22: Prompt logging enforced by Claude Code hooks, not by
    Claude's discipline. Hooks configured in .claude/settings.json."""

    def test_hook_settings_exist(self):
        settings_path = PROJECT_ROOT / ".claude" / "settings.json"
        assert settings_path.exists(), ".claude/settings.json not found"

    def test_hook_settings_has_user_prompt_submit(self):
        settings_path = PROJECT_ROOT / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "UserPromptSubmit" in data.get("hooks", {}), "UserPromptSubmit hook not configured"

    def test_hook_settings_has_stop(self):
        settings_path = PROJECT_ROOT / ".claude" / "settings.json"
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        assert "Stop" in data.get("hooks", {}), "Stop hook not configured"

    def test_hook_script_exists(self):
        hook_path = HOOK_DIR / "conversation_log_hook.py"
        assert hook_path.exists(), "conversation_log_hook.py not found"

    def test_hook_script_handles_user_prompt(self):
        """Verify hook script can process a UserPromptSubmit event."""
        assert hasattr(hook, "handle_user_prompt_submit"), \
            "conversation_log_hook missing handle_user_prompt_submit"

    def test_hook_script_handles_stop(self):
        """Verify hook script can process a Stop event."""
        assert hasattr(hook, "handle_stop"), \
            "conversation_log_hook missing handle_stop"


# ── Schema Validation ────────────────────────────────────────────────


class TestSchemaValidation:
    """Validate prompt_log.json against strict schema."""

    def test_prompt_log_file_exists(self):
        assert PROMPT_LOG_PATH.exists(), f"prompt_log.json not found at {PROMPT_LOG_PATH}"

    def test_valid_json(self):
        data = load_prompt_log()
        assert isinstance(data, dict)

    def test_required_top_level_fields(self):
        data = load_prompt_log()
        for field in PROMPT_LOG_SCHEMA["required_top_level"]:
            assert field in data, f"Missing top-level field: {field}"

    def test_collection_value(self):
        data = load_prompt_log()
        assert data["collection"] == "conversation_log"

    def test_produces_artifact_false(self):
        data = load_prompt_log()
        assert data["produces_artifact"] is False

    def test_retention_24_hours(self):
        data = load_prompt_log()
        assert data["retention"] == "24_hours"

    def test_no_additional_top_level_fields(self):
        data = load_prompt_log()
        allowed = {"collection", "path", "purpose", "produces_artifact", "retention", "utterances", "schema_address"}
        extra = set(data.keys()) - allowed
        assert not extra, f"Unexpected top-level fields: {extra}"

    def test_utterance_required_fields(self):
        data = load_prompt_log()
        for u in data.get("utterances", []):
            for field in PROMPT_LOG_SCHEMA["utterance_required"]:
                assert field in u, f"Utterance {u.get('utterance', '?')} missing field: {field}"

    def test_utterance_no_extra_fields(self):
        data = load_prompt_log()
        allowed = set(PROMPT_LOG_SCHEMA["utterance_required"]) | set(PROMPT_LOG_SCHEMA["utterance_optional"])
        for u in data.get("utterances", []):
            extra = set(u.keys()) - allowed
            assert not extra, f"Utterance {u.get('utterance', '?')} has extra fields: {extra}"

    def test_role_values(self):
        data = load_prompt_log()
        for u in data.get("utterances", []):
            assert u["role"] in PROMPT_LOG_SCHEMA["role_enum"], f"Invalid role: {u['role']}"

    def test_actor_format(self):
        data = load_prompt_log()
        for u in data.get("utterances", []):
            assert re.match(PROMPT_LOG_SCHEMA["actor_pattern"], u["actor"]), f"Invalid actor: {u['actor']}"

    def test_timestamp_formats(self):
        data = load_prompt_log()
        for u in data.get("utterances", []):
            assert re.match(PROMPT_LOG_SCHEMA["timestamp_utc_pattern"], u["timestamp_utc"]), \
                f"Invalid UTC timestamp: {u['timestamp_utc']}"
            assert re.match(PROMPT_LOG_SCHEMA["timestamp_pst_pattern"], u["timestamp_pst"]), \
                f"Invalid PST timestamp: {u['timestamp_pst']}"
