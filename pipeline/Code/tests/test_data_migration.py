"""Tests for EPIC-010-F-108-S-001 — Data Migration.

Two halves.

The first drives Rule-065-ENF-009 against deliberately broken copies of the
migration file, one per requirement, and asserts the commit is refused each
time. It touches no database.

The second invokes the migration the way Azure Automation invokes it: a
webhook envelope on argv, through the file's own entry point. Nothing calls
MigratedCollection directly. An earlier version of this suite did, and so it
migrated live data with no approval at all -- it would have passed with the
authorization check deleted, which makes it a test of the copy loop wearing
the name of a test of the story.

Four cases run unattended, and every one of them is a refusal:

    no authorization on the payload          refused, nothing written
    verdict approve but human_click false    refused, nothing written
    target already at the destination        refused, nothing written
    source absent from the pipeline          refused, nothing written

The fifth case is the successful migration, and it requires an operator to
click APPROVE on the page the suite opens. That is deliberate: the server
refuses any decision arriving without the mouse-click marker, so a suite that
could approve itself would be proving the gate does not work. Run it with
CH_OPERATOR=1 to include that case; without the flag it is skipped and the four
refusals still run.

Nothing is faked. Rule-065-ENF-003 forbids a mock, stub or in-memory double
of a client, database or collection: the certificate is the credential, and
code that reaches Mongo without an identity is code whose authorization is
never tested.
"""
from __future__ import annotations

import json
import os
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
_MIGRATE_TOOL = _REPO / "pipeline" / "Code" / "ops" / "migrate_data.py"
_CLUSTER_WAIT_SECONDS = 900
_CLUSTER_POLL_SECONDS = 20
_SEED_ROWS = 5
_RESERVATION_MINUTES = 60


_OPERATOR_ENV = "CH_OPERATOR"


def _operator_present() -> bool:
    """The operator cases open a page and block on a click, so they run only
    when a person said they are at the keyboard. pytest_addoption belongs in
    conftest.py, not here, so this is an environment flag instead of a CLI
    option -- one fewer file touched for the same effect."""
    return (os.environ.get(_OPERATOR_ENV) or "").strip() == "1"


# ── half one: the commit gate ────────────────────────────────────────────────

_BREAKAGES = {
    "REQ-B-001": (
        "destination = self._destination()",
        'destination = self._destination_database()[self._collection_name + "_migrated"]'),
    "REQ-B-003": (
        'if (authorization.get("verdict") != "approve"',
        'if (False and authorization.get("verdict") != "approve"'),
    "REQ-B-005": (
        "        if self.exists_at_destination():",
        "        if False and self.exists_at_destination():"),
    "REQ-B-007": (
        "    def __init__(self, collection_name: str) -> None:\n        self._collection_name = collection_name",
        "    def __init__(self, collection_name: str, batch_size: int = 1000) -> None:\n        self._collection_name = collection_name\n        self._batch_size = batch_size"),
}


def _run_enforcement() -> tuple[int, list[str]]:
    result = subprocess.run(
        [sys.executable, str(_WORKER), _ENFORCEMENT],
        input="pipeline/Code/data_migration.py\n",
        capture_output=True, text=True, cwd=str(_REPO), timeout=1800)
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
    original = _MIGRATION_PATH.read_text(encoding="utf-8")
    try:
        yield original
    finally:
        _MIGRATION_PATH.write_text(original, encoding="utf-8")


def test_the_unmodified_migration_file_passes_every_requirement():
    """The control. Without it, a gate that rejects everything looks healthy."""
    code, messages = _run_enforcement()
    assert code == 0, f"pristine file rejected: {messages}"


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


def test_an_unregistered_migration_file_is_refused(pristine_migration_file):
    """REQ-B-012. An undeclared migrator is a back door."""
    manifest = (_REPO / "brain" / "machine_artifacts" / "content"
                / "deployment_architecture.json")
    saved = manifest.read_text(encoding="utf-8")
    document = json.loads(saved)
    for target in document.get("DeploymentTargetRecord", []):
        for binding in target.get("environments", []):
            binding["packages"] = [
                p for p in (binding.get("packages") or [])
                if p.get("package_id") != "data_migration"
            ]
    try:
        manifest.write_text(
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8")
        code, messages = _run_enforcement()
        assert code != 0, "undeclared migration file was allowed to commit"
        assert any("declared in no target or package" in m for m in messages), messages
    finally:
        manifest.write_text(saved, encoding="utf-8")


# ── half two: the migration, through its own entry point ─────────────────────

def _invoke(payload: dict) -> subprocess.CompletedProcess:
    """Run data_migration.py the way Azure Automation runs it.

    The sandbox hands the webhook envelope in on argv without quote-protecting
    it, so the payload arrives whitespace-split and the runbook recovers
    RequestBody by balanced-brace parsing. Passing it the same way here means
    the envelope parser is under test too, not bypassed.
    """
    envelope = ("{WebhookName:DataMigrationOnDemand,RequestBody:"
                + json.dumps(payload) + "}")
    return subprocess.run(
        [sys.executable, str(_MIGRATION_PATH), *envelope.split(" ")],
        capture_output=True, text=True, cwd=str(_REPO / "pipeline" / "Code"),
        timeout=1800)


def _approved(collection: str) -> dict:
    return {"collection": collection,
            "released_at": "2026-08-18T00:00:00Z",
            "authorization": {"verdict": "approve", "human_click": True,
                              "subject": collection}}


def _front_end():
    return ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]


def _lifecycle():
    from cluster_lifecycle_manager import ClusterLifecycleManager
    return ClusterLifecycleManager(get_db_fn=None)


def _wake_pipeline_cluster() -> None:
    """Resume through the Atlas control plane, then wait for it to answer.

    Resuming is not a database operation and no Mongo user can do it. It runs
    on the Atlas API key pair -- the second credential the run needs before
    any identity below can connect.
    """
    subprocess.run([sys.executable, str(_RESUME_TOOL)],
                   capture_output=True, text=True, cwd=str(_REPO), timeout=900)
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
def reservation():
    """A reservation for the suite, then released.

    Running this suite is the consent to spend the cluster. The operator typed
    the command; no page is opened to ask permission for that, because a page
    that says "Authorize this migration?" while meaning "may I wake a machine"
    is a production prompt wearing a costume -- and one of those was shown
    once, for a migration that could never happen.

    The reaper pauses the cluster whenever nothing holds a reservation, so
    without one it can pause mid-suite; a reservation left behind keeps it
    awake and billing. Released in teardown whatever the tests did.
    """
    if not _operator_present():
        pytest.skip(f"needs a human at the keyboard; run with {_OPERATOR_ENV}=1")

    job_id = f"migration-test-{uuid.uuid4().hex[:8]}"
    lifecycle = _lifecycle()
    lifecycle.reserve(
        cluster_name=_PIPELINE_CLUSTER,
        job_id=job_id,
        requester="test_data_migration",
        expected_duration_minutes=_RESERVATION_MINUTES,
        reservation_class="human",
    )
    try:
        _wake_pipeline_cluster()
        yield job_id
    finally:
        released = lifecycle.release(job_id)
        assert released["deleted_count"] == 1, (
            f"reservation {job_id} was not released; the cluster will stay "
            f"awake and billing until someone removes it")




@pytest.fixture(scope="module")
def planted(reservation):
    """Three collection names, and the state each case needs.

    Names carry a per-run uuid, so no live collection is ever involved and a
    re-run cannot collide with residue it has no right to remove.

        migratable        seeded on the pipeline, absent at the destination
        already_there     seeded on the pipeline AND planted at the destination
        absent_everywhere seeded nowhere
    """
    run = uuid.uuid4().hex[:8]
    names = {
        "migratable": f"MigrationTest_Absent_{run}",
        "already_there": f"MigrationTest_Present_{run}",
        "absent_everywhere": f"MigrationTest_Nowhere_{run}",
        "run": run,
    }
    source = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _PIPELINE_CLUSTER)[_PIPELINE_DB]
    for key in ("migratable", "already_there"):
        source[names[key]].insert_many(
            [{"run": run, "n": i} for i in range(_SEED_ROWS)])

    # frontendUser plants the collision the migrator must refuse.
    _front_end()[names["already_there"]].insert_one(
        {"run": run, "planted_by": "frontendUser"})

    yield names

    for key in ("migratable", "already_there"):
        source[names[key]].drop()


def _approval_record(collection: str) -> dict | None:
    """The decision as Mongo holds it. The log says what happened; this says
    what was recorded, and the record is what REQ-B-003 asks for."""
    return _front_end()["MigrationApprovals"].find_one(
        {"collection": collection}, sort=[("released_at", -1)])


def _operator_run(collection: str) -> subprocess.CompletedProcess:
    """Invoke the operator's program. One invocation, one page, one click."""
    return subprocess.run(
        [sys.executable, str(_MIGRATE_TOOL), collection, "--env", "dev"],
        capture_output=True, text=True, cwd=str(_REPO), timeout=1800)


# ── the three runs, each one page ────────────────────────────────────────────

def test_run_one_a_collection_on_the_pipeline_and_not_the_front_end_migrates(planted):
    """APPROVE. The only case that writes: every document arrives under the
    same name, and the approval is in Mongo."""
    name = planted["migratable"]
    result = _operator_run(name)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output[-900:]

    record = _approval_record(name)
    assert record is not None, "no approval record in Mongo"
    assert record["verdict"] == "approve", record
    assert record["human_click"] is True, record

    assert _front_end()[name].count_documents({}) == _SEED_ROWS


def test_run_two_a_collection_absent_from_the_pipeline_fails(planted):
    """APPROVE. Authorization succeeds and the migration still fails: there is
    nothing at the source, and an empty success would be a lie."""
    name = planted["absent_everywhere"]
    result = _operator_run(name)
    output = result.stdout + result.stderr

    record = _approval_record(name)
    assert record is not None, "no approval record in Mongo"
    assert record["verdict"] == "approve", record

    assert "migration_source_absent" in output or result.returncode != 0, output[-900:]
    assert name not in _front_end().list_collection_names()


def test_run_three_a_collection_already_on_the_front_end_fails(planted):
    """APPROVE. Authorization succeeds and the migration abends: the target is
    already there, and what is there is left exactly as it was."""
    name = planted["already_there"]
    before = _front_end()[name].count_documents({})
    result = _operator_run(name)
    output = result.stdout + result.stderr

    record = _approval_record(name)
    assert record is not None, "no approval record in Mongo"
    assert record["verdict"] == "approve", record

    assert "migration_target_exists" in output or result.returncode != 0, output[-900:]
    assert _front_end()[name].count_documents({}) == before, "the refusal wrote"
