"""Unit tests for the spool writer and drain.

No MongoDB, no network, no real spool directory -- every test writes into tmp_path
and inserts into a fake collection.

Every payload these tests generate carries TEST_MARKER in its content, so if any
of it ever does reach the archive it can be found and purged:

    db.conversation_log_archive.deleteMany(
        {"content": {"$regex": "CHATHEALTHY_SPOOL_UNIT_TEST"}})
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import conversation_log_drain as drain          # noqa: E402
import conversation_log_writer as writer        # noqa: E402


class FakeCollection:
    """Records inserts. raise_on lets a test make MongoDB fail."""

    def __init__(self, raise_on: int | None = None):
        self.docs: list[dict] = []
        self.calls = 0
        self.raise_on = raise_on

    def insert_one(self, doc):
        self.calls += 1
        if self.raise_on is not None and self.calls == self.raise_on:
            raise RuntimeError("mongo unreachable")
        self.docs.append(doc)


# Stamped into every generated payload so test data is identifiable in the
# archive and can be purged by content. Never change this without changing the
# purge query in the module docstring.
TEST_MARKER = "CHATHEALTHY_SPOOL_UNIT_TEST"


def payload_bytes(prompt="hello") -> bytes:
    return json.dumps({
        "hook_event_name": "UserPromptSubmit",
        "prompt": f"{TEST_MARKER} {prompt}",
    }).encode()


# ── writer ────────────────────────────────────────────────────────────────────

def test_writer_lands_a_read_file(tmp_path):
    path = writer.write_payload(payload_bytes(), tmp_path)
    assert path.exists()
    assert path.name.startswith(writer.READ_PREFIX)
    assert path.read_bytes() == payload_bytes()


def test_writer_leaves_no_write_file_behind(tmp_path):
    writer.write_payload(payload_bytes(), tmp_path)
    assert [p for p in tmp_path.iterdir() if p.name.startswith(writer.WRITE_PREFIX)] == []


def test_writer_does_not_alter_the_payload(tmp_path):
    """The writer must copy bytes, never parse and re-serialise."""
    raw = b'{"prompt": "\\u00e9\\u00e8", "trailing": "space" }   '
    path = writer.write_payload(raw, tmp_path)
    assert path.read_bytes() == raw


def test_parallel_writers_never_collide(tmp_path):
    paths = {writer.write_payload(payload_bytes(f"m{i}"), tmp_path) for i in range(200)}
    assert len(paths) == 200
    assert len(list(tmp_path.iterdir())) == 200


def test_filename_carries_a_parsable_time(tmp_path):
    before = time.time_ns()
    path = writer.write_payload(payload_bytes(), tmp_path)
    after = time.time_ns()
    guid, time_ns = drain.parse_spool_name(path.name)
    assert guid
    assert before <= time_ns <= after


# ── name parsing ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", [
    "write-abc-00000000000000000001.txt",   # not yet renamed
    "read-abc.txt",                         # no time
    "read-abc-notanumber.txt",              # unparsable time
    "read-abc-123.json",                    # wrong suffix
    "something-else.txt",
])
def test_malformed_names_are_skipped_not_guessed(name):
    assert drain.parse_spool_name(name) is None


def test_drain_ignores_files_still_being_written(tmp_path):
    (tmp_path / "write-abc-00000000000000000001.txt").write_bytes(payload_bytes())
    assert drain.ready_files(tmp_path) == []


# ── drain ─────────────────────────────────────────────────────────────────────

def test_drain_ships_and_deletes(tmp_path):
    writer.write_payload(payload_bytes(), tmp_path)
    coll = FakeCollection()
    shipped, failed = drain.drain_once(tmp_path, coll)
    assert (shipped, failed) == (1, 0)
    assert len(coll.docs) == 1
    assert list(tmp_path.iterdir()) == []


def test_timestamp_comes_from_the_filename(tmp_path):
    path = writer.write_payload(payload_bytes(), tmp_path)
    _, time_ns = drain.parse_spool_name(path.name)
    coll = FakeCollection()
    drain.drain_once(tmp_path, coll)
    from datetime import datetime, timezone
    written = coll.docs[0]["timestamp"]
    expected = datetime.fromtimestamp(time_ns / 1e9, tz=timezone.utc)
    assert written == expected


def test_timestamp_is_a_native_utc_instant(tmp_path):
    """Stored as a BSON date, not a formatted string: an index over it compares
    instants, so ordering survives daylight-saving transitions."""
    from datetime import datetime, timezone
    writer.write_payload(payload_bytes(), tmp_path)
    coll = FakeCollection()
    drain.drain_once(tmp_path, coll)
    written = coll.docs[0]["timestamp"]
    assert isinstance(written, datetime)
    assert written.tzinfo is timezone.utc


def test_dropped_fields_are_absent(tmp_path):
    """_version, _framework and _build were removed from the archive on the
    port; new records must not reintroduce them."""
    writer.write_payload(payload_bytes(), tmp_path)
    coll = FakeCollection()
    drain.drain_once(tmp_path, coll)
    for field in drain.DROPPED_FIELDS:
        assert field not in coll.docs[0]


def test_record_keeps_the_archive_shape(tmp_path):
    writer.write_payload(payload_bytes("find me a doctor"), tmp_path)
    coll = FakeCollection()
    drain.drain_once(tmp_path, coll)
    doc = coll.docs[0]
    for field in ("timestamp", "archived_at", "role", "content"):
        assert field in doc, f"{field} missing -- the boot reads this collection"
    for gone in ("ch_key", "timestamp_pst", "timestamp_utc", "actor"):
        assert gone not in doc, f"{gone} is from the pre-rewrite shape"
    assert doc["role"] == "user"
    assert doc["content"] == f"{TEST_MARKER} find me a doctor"


def test_file_survives_a_failed_insert(tmp_path):
    """The durability contract: delete only after MongoDB acknowledges."""
    writer.write_payload(payload_bytes(), tmp_path)
    coll = FakeCollection(raise_on=1)
    shipped, failed = drain.drain_once(tmp_path, coll)
    assert (shipped, failed) == (0, 1)
    assert len(drain.ready_files(tmp_path)) == 1


def test_failed_file_ships_on_the_next_pass(tmp_path):
    writer.write_payload(payload_bytes(), tmp_path)
    coll = FakeCollection(raise_on=1)
    drain.drain_once(tmp_path, coll)
    shipped, failed = drain.drain_once(tmp_path, coll)
    assert (shipped, failed) == (1, 0)
    assert list(tmp_path.iterdir()) == []


def test_one_bad_file_does_not_block_the_rest(tmp_path):
    for i in range(5):
        writer.write_payload(payload_bytes(f"m{i}"), tmp_path)
    coll = FakeCollection(raise_on=3)
    shipped, failed = drain.drain_once(tmp_path, coll)
    assert (shipped, failed) == (4, 1)
    assert len(drain.ready_files(tmp_path)) == 1


def test_drain_of_empty_spool_is_a_no_op(tmp_path):
    coll = FakeCollection()
    assert drain.drain_once(tmp_path, coll) == (0, 0)
    assert coll.docs == []


def test_backlog_survives_a_long_outage(tmp_path):
    for i in range(50):
        writer.write_payload(payload_bytes(f"m{i}"), tmp_path)
    down = FakeCollection(raise_on=1)
    for _ in range(3):
        down.calls = 0                      # fails every pass
        down.raise_on = 1
    assert len(drain.ready_files(tmp_path)) == 50   # nothing lost while down
    shipped, failed = drain.drain_once(tmp_path, FakeCollection())
    assert (shipped, failed) == (50, 0)


# ── health signal ─────────────────────────────────────────────────────────────

def test_oldest_age_is_zero_when_drained(tmp_path):
    assert drain.oldest_ready_age_seconds(tmp_path, time.time_ns()) == 0.0


def test_oldest_age_grows_with_backlog(tmp_path):
    writer.write_payload(payload_bytes(), tmp_path)
    future = time.time_ns() + 60_000_000_000        # 60s later
    assert drain.oldest_ready_age_seconds(tmp_path, future) >= 59


def test_oldest_age_reports_the_oldest_not_the_newest(tmp_path):
    old = tmp_path / f"read-aaa-{1:020d}.txt"
    old.write_bytes(payload_bytes())
    writer.write_payload(payload_bytes(), tmp_path)
    assert drain.oldest_ready_age_seconds(tmp_path, time.time_ns()) > 1e9   # the epoch-old file


# ── orphan sweep ──────────────────────────────────────────────────────────────

def test_sweep_removes_stale_write_files(tmp_path):
    orphan = tmp_path / "write-abc-00000000000000000001.txt"
    orphan.write_bytes(b"half")
    import os
    old = time.time() - 3600
    os.utime(orphan, (old, old))
    assert drain.sweep_orphans(tmp_path) == 1
    assert not orphan.exists()


def test_sweep_spares_a_write_in_progress(tmp_path):
    fresh = tmp_path / "write-abc-00000000000000000001.txt"
    fresh.write_bytes(b"being written right now")
    assert drain.sweep_orphans(tmp_path) == 0
    assert fresh.exists()


def test_sweep_never_touches_ready_files(tmp_path):
    path = writer.write_payload(payload_bytes(), tmp_path)
    import os
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert drain.sweep_orphans(tmp_path) == 0
    assert path.exists()
