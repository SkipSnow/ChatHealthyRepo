"""Tests for EPIC-010-F-108-S-001 — Data Migration.

Two halves.

The first drives Rule-065-ENF-009 against deliberately broken copies of the
migration file, one per requirement, and asserts the commit is refused each
time. It touches no database.

The second exercises the real identities against the real clusters. It wakes
the pipeline cluster through the Atlas control plane — a different credential
from any Mongo user, because resuming a cluster is not a database operation —
waits for it to answer, builds two collections as pipelineEditor, plants one
of them at the destination as frontendUser, and then puts three cases to the
migrator:

    already at the destination   -> must refuse
    at the source only           -> must succeed
    at neither                   -> must refuse

Nothing is faked. Rule-065-ENF-003 forbids a mock, stub or in-memory double
of a client, database or collection, and the certificate is the credential:
code that reaches Mongo without an identity is code whose authorization is
never tested.

Teardown is honest rather than silent. dataMigrator is append-only and
frontendUser holds neither delete nor drop, so nothing in the estate can
remove what these tests write to the serving database. The teardown says so
and fails, because a test suite that quietly leaks collections into the
database serving users is worse than one that refuses to pretend.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import time
import uuid

import pytest

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / ".git").is_dir():
        _REPO = _parent
        break
sys.path.insert(0, str(_REPO / "pipeline" / "Code"))
sys.path.insert(0, str(_REPO / "ChatHealthyLib" / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(str(_REPO / ".env"), override=False)

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

STORY = "EPIC-010-F-108-S-001"

_MIGRATION_PATH = _REPO / "pipeline" / "Code" / "data_migration.py"
_WORKER = (_REPO / "architecture" / "EngineeringRuleEnforcement" / "code"
           / "scan_data_migration_compliance_worker.py")
_ENFORCEMENT = "Rule-065-ENF-009"

_PIPELINE_CLUSTER = "ChatHealthyDataPipelines"
_FRONT_END_CLUSTER = "ChatHealthyFrontEnd"
_PIPELINE_DB = "PipelinePublicHealthData"
_FRONT_END_DB = "PublicHealthData"

_RESUME_TOOL = _REPO / "pipeline" / "Code" / "ops" / "pipeline_resume_cluster.py"
_CLUSTER_WAIT_SECONDS = 900
_CLUSTER_POLL_SECONDS = 20


# ── half one: the commit gate ────────────────────────────────────────────────

# One mutation per requirement. Each is a plausible edit that a person could
# make without noticing they had broken the story.
_BREAKAGES = {
    "REQ-B-001": (
        'destination = self._destination()',
        'destination = self._destination_database()[self._collection_name + "_migrated"]'),
    "REQ-B-004": (
        "destination.insert_many(batch, ordered=False)\n                written += len(batch)\n                batch = []",
        "destination.delete_many({})\n                destination.insert_many(batch, ordered=False)\n                written += len(batch)\n                batch = []"),
    "REQ-B-005": (
        "        if self.exists_at_destination():",
        "        if False and self.exists_at_destination():"),
    "REQ-B-006": (
        "    def _source(self):\n        return ChatHealthyMongoUtilities().getConnection(",
        "    def _source(self):\n        return _loose_handle()\n\n    def _unused_source(self):\n        return ChatHealthyMongoUtilities().getConnection("),
    "REQ-B-007": (
        "    def __init__(self, collection_name: str) -> None:\n        self._collection_name = collection_name",
        "    def __init__(self, collection_name: str, batch_size: int = 1000) -> None:\n        self._collection_name = collection_name\n        self._batch_size = batch_size"),
    "REQ-B-003": (
        'if (authorization.get("verdict") != "approve"',
        'if (False and authorization.get("verdict") != "approve"'),
}


def _run_enforcement() -> tuple[int, list[str]]:
    """Run the worker over the migration file. Returns (exit code, messages)."""
    result = subprocess.run(
        [sys.executable, str(_WORKER), _ENFORCEMENT],
        input="pipeline/Code/data_migration.py\n",
        capture_output=True, text=True, cwd=str(_REPO), timeout=900)
    messages = []
    for line in (result.stdout or "").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("kind") == "violation":
            messages.append(record.get("message", ""))
    return result.returncode, messages


@pytest.fixture
def pristine_migration_file():
    """Restore the migration file whatever the test does to it."""
    original = _MIGRATION_PATH.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        _MIGRATION_PATH.write_text(original, encoding="utf-8")


def test_the_unmodified_migration_file_passes_every_requirement():
    """The control. Without it, a gate that rejects everything looks healthy."""
    code, messages = _run_enforcement()
    assert code == 0, f"pristine file rejected: {messages}"
    assert messages == []


@pytest.mark.parametrize("req_id", sorted(_BREAKAGES))
def test_each_broken_requirement_is_refused(req_id, pristine_migration_file):
    """REQ-B-010. Break one requirement; the commit must not be made."""
    original = pristine_migration_file
    old, new = _BREAKAGES[req_id]
    assert old in original, f"{req_id} mutation no longer matches the file"
    _MIGRATION_PATH.write_text(original.replace(old, new, 1), encoding="utf-8")

    code, messages = _run_enforcement()
    assert code != 0, f"{req_id} broken but the gate passed it"
    assert messages, f"{req_id} broken but nothing was reported"


def test_an_unregistered_migration_file_is_refused(pristine_migration_file, tmp_path):
    """REQ-B-012. The manifest declaration is what stops a back door; a file
    that migrates and is registered nowhere must not commit."""
    manifest = _REPO / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    saved = manifest.read_text(encoding="utf-8")
    document = json.loads(saved)
    for target in document.get("DeploymentTargetRecord", []):
        for binding in target.get("environments", []):
            binding["packages"] = [
                p for p in (binding.get("packages") or [])
                if p.get("package_id") != "data_migration"
            ]
    try:
        manifest.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        code, messages = _run_enforcement()
        assert code != 0, "undeclared migration file was allowed to commit"
        assert any("declared in no target or package" in m for m in messages), messages
    finally:
        manifest.write_text(saved, encoding="utf-8")


# ── half two: the real clusters ──────────────────────────────────────────────

def _wake_pipeline_cluster() -> None:
    """Resume through the Atlas control plane and wait for it to answer.

    Resuming is not a database operation and no Mongo user can do it, so this
    runs on the Atlas API key pair -- the separate credential the run needs
    before any of the identities below can connect.
    """
    subprocess.run(
        [sys.executable, str(_RESUME_TOOL)],
        capture_output=True, text=True, cwd=str(_REPO), timeout=600)

    deadline = time.monotonic() + _CLUSTER_WAIT_SECONDS
    last = None
    while time.monotonic() < deadline:
        try:
            client = ChatHealthyMongoUtilities().getConnection(
                "pipelineEditor", _PIPELINE_CLUSTER)
            client.admin.command("ping")
            return
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(_CLUSTER_POLL_SECONDS)
    pytest.fail(f"pipeline cluster did not answer within "
                f"{_CLUSTER_WAIT_SECONDS}s: {type(last).__name__}: {last}")


@pytest.fixture(scope="module")
def planted_collections():
    """Two collections on the pipeline cluster, one of them also planted at
    the destination. Names are unique per run so a rerun never collides with
    residue it cannot remove."""
    _wake_pipeline_cluster()
    run = uuid.uuid4().hex[:8]
    already_there = f"MigrationTest_Present_{run}"
    migratable = f"MigrationTest_Absent_{run}"
    absent_everywhere = f"MigrationTest_Nowhere_{run}"

    source = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _PIPELINE_CLUSTER)[_PIPELINE_DB]
    for name in (already_there, migratable):
        source[name].insert_many([{"run": run, "n": i} for i in range(5)])

    # frontendUser plants the collision. dataMigrator must refuse this one.
    front = ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]
    front[already_there].insert_one({"run": run, "planted_by": "frontendUser"})

    yield {"already_there": already_there,
           "migratable": migratable,
           "absent_everywhere": absent_everywhere,
           "run": run}

    # The pipeline side cleans up. pipelineEditor holds drop there.
    for name in (already_there, migratable):
        source[name].drop()


def test_migration_refuses_a_collection_already_at_the_destination(planted_collections):
    """REQ-B-005. Present at the destination -> raise, and write nothing."""
    from data_migration import MigratedCollection

    name = planted_collections["already_there"]
    front = ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]
    before = front[name].count_documents({})

    migrated = MigratedCollection(name)
    with pytest.raises(ChatHealthyException) as raised:
        migrated.refuse_unless_migratable()
    assert raised.value.mode == "migration_target_exists"
    assert name in str(raised.value)
    assert front[name].count_documents({}) == before, "the refusal still wrote"


def test_migration_moves_a_collection_absent_from_the_destination(planted_collections):
    """REQ-B-001 and REQ-B-004. Every document arrives, under the same name."""
    from data_migration import MigratedCollection

    name = planted_collections["migratable"]
    migrated = MigratedCollection(name)
    migrated.refuse_unless_migratable()
    expected = migrated.source_count()
    assert expected == 5

    written = 0
    for written in migrated.copy():
        pass
    assert written == expected

    front = ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]
    assert front[name].count_documents({}) == expected


def test_migration_refuses_a_collection_absent_from_the_pipeline(planted_collections):
    """REQ-B-005's mirror. Nothing to migrate is a failure, not an empty win."""
    from data_migration import MigratedCollection

    name = planted_collections["absent_everywhere"]
    migrated = MigratedCollection(name)
    with pytest.raises(ChatHealthyException) as raised:
        migrated.refuse_unless_migratable()
    assert raised.value.mode == "migration_source_absent"


def test_the_estate_can_remove_what_these_tests_wrote(planted_collections):
    """REQ-B-004 protects existing data by making the migrator append-only.
    The cost is that nothing here can clean up, and that cost is real: these
    collections stay in the database serving users until an admin removes
    them. This test asserts the estate can undo its own test writes, and it
    fails until some identity holds dropCollection on the serving database.
    """
    front = ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]
    leaked = [planted_collections["already_there"], planted_collections["migratable"]]
    unremovable = []
    for name in leaked:
        try:
            front[name].drop()
        except Exception:  # noqa: BLE001
            unremovable.append(name)
    assert not unremovable, (
        "no identity in the estate can drop these from "
        f"{_FRONT_END_CLUSTER}.{_FRONT_END_DB}: {unremovable}. dataMigrator is "
        "append-only by design and frontendUser holds neither delete nor drop, "
        "so every run of this suite leaks collections into the serving "
        "database permanently.")
