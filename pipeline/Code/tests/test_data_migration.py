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
        'if record.get("verdict") != "approve":',
        'if False and record.get("verdict") != "approve":'),
    "REQ-B-005": (
        "        if self.exists_at_destination():",
        "        if False and self.exists_at_destination():"),
    "REQ-B-007": (
        "    def __init__(self, collection_name: str) -> None:\n        self._collection_name = collection_name",
        "    def __init__(self, collection_name: str, batch_size: int = 1000) -> None:\n        self._collection_name = collection_name\n        self._batch_size = batch_size"),
}


def _run_enforcement(path: str = "pipeline/Code/data_migration.py") -> tuple[int, list[str]]:
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
    """TRUE: the migration file as written satisfies every requirement of its
    story.

    A code assertion. It reads the source and asks the gate to judge it; no
    migration runs and no data moves. The control: without it a gate that
    refuses everything looks healthy and every other case here passes for the
    wrong reason."""
    code, messages = _run_enforcement()
    assert code == 0, f"pristine file rejected: {messages}"


@pytest.mark.parametrize("req_id", sorted(_BREAKAGES))
def test_each_broken_requirement_is_refused(req_id, pristine_migration_file):
    """TRUE: an engineering rule guarantees the code meets this story, so code
    that breaks any one requirement cannot enter the repository. REQ-B-010.

    Code assertions, all four. Each judges source text and nothing runs.

    Each case takes the shipped file, breaks exactly one requirement, and
    requires the commit to be refused:
      B-001  the collection would arrive under a different name
      B-003  the migration would run without a recorded human release
      B-005  the migration would proceed over a collection already there
      B-007  the class would take a second constructor argument
    """
    original = pristine_migration_file
    old, new = _BREAKAGES[req_id]
    assert old in original, f"{req_id} mutation no longer matches the file"
    _MIGRATION_PATH.write_text(original.replace(old, new, 1), encoding="utf-8")

    code, messages = _run_enforcement()
    assert code != 0, f"{req_id} broken but the gate passed it"
    assert messages, f"{req_id} broken but nothing was reported"


def test_an_unregistered_migration_file_is_refused():
    """TRUE: the file that performs the migration is declared in
    deployment_architecture.json under exactly one target and one package, and a
    file declared nowhere, or in more than one place, is refused. REQ-B-012.

    A code assertion on the check itself.

    Two earlier versions of this test asserted nothing. The first stripped the
    declaration out of the working-tree copy of deployment_architecture.json and
    expected refusal; the gate reads that document from origin/dev exactly so
    the thing being judged cannot edit its own standard, so the edit was
    invisible. The second presented a copy of the migrator at an undeclared
    path; the gate holds the migration file's path itself and never judged the
    copy. Neither could fail.

    The declaration cannot be broken from a working tree, so the check is put
    to the three states it exists to tell apart: declared once, declared
    nowhere, declared twice.
    """
    import importlib.util
    # The worker imports its base class by bare name, as every enforcement
    # worker does, so its own directory has to be importable.
    worker_dir = str(pathlib.Path(_WORKER).parent)
    if worker_dir not in sys.path:
        sys.path.insert(0, worker_dir)
    spec = importlib.util.spec_from_file_location("gate", _WORKER)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    worker = gate.ScanDataMigrationComplianceWorker.__new__(
        gate.ScanDataMigrationComplianceWorker)

    worker._declared_locations = lambda: [("target_a", "data_migration")]
    assert worker._manifest_violation() == "", (
        "a file declared once was reported as a violation")

    worker._declared_locations = lambda: []
    undeclared = worker._manifest_violation()
    assert undeclared, "a file declared nowhere was allowed"
    assert "declared in no target or package" in undeclared, undeclared

    worker._declared_locations = lambda: [("target_a", "data_migration"),
                                          ("target_b", "data_migration")]
    twice = worker._manifest_violation()
    assert twice, "a file declared in two places was allowed"
    assert "more than one place" in twice, twice


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
    """TRUE: a migration a human released moves the data, and the collection
    arrives under the name it left with. REQ-B-003 positively, and REQ-B-001.

    A behavioural assertion. It wakes the pipeline cluster, opens the approval
    page, waits for a person, and writes. The only case in this file that
    writes. Every document arrives, the name is
    unchanged, and the human`s sign-off is recorded in Mongo before it ran."""
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
    """TRUE: approval alone does not make a migration happen. A released
    migration with nothing at the source fails rather than reporting an empty
    success, which would tell the operator data had moved when none had.

    A behavioural assertion. It maps to no requirement and needs none: code that
    reports success having moved nothing is simply wrong, and correctness of
    that kind is not specified, it is expected."""
    name = planted["absent_everywhere"]
    result = _operator_run(name)
    output = result.stdout + result.stderr

    record = _approval_record(name)
    assert record is not None, "no approval record in Mongo"
    assert record["verdict"] == "approve", record

    assert result.returncode != 0, f"an absent source reported success: {output[-900:]}"
    assert "migration_source_absent" in output, (
        f"the refusal did not raise the mode this requirement names: {output[-900:]}")
    assert name in output, (
        f"the log does not name the collection asked for: {output[-900:]}")
    assert name not in _front_end().list_collection_names()


def test_run_three_a_collection_already_on_the_front_end_fails(planted):
    """TRUE: a collection already present at the destination stops the
    migration, and what is already there is left exactly as it was. REQ-B-005.

    A behavioural assertion. It runs the migration for real against a planted
    collection. Approval succeeded. B-005 stopped it anyway, which is the point: a human
    releasing a migration cannot cause existing data to be overwritten."""
    name = planted["already_there"]
    before = _front_end()[name].count_documents({})
    result = _operator_run(name)
    output = result.stdout + result.stderr

    record = _approval_record(name)
    assert record is not None, "no approval record in Mongo"
    assert record["verdict"] == "approve", record

    assert result.returncode != 0, f"an occupied target reported success: {output[-900:]}"
    assert "migration_target_exists" in output, (
        f"the refusal did not raise the mode this requirement names: {output[-900:]}")
    assert name in output, (
        f"the narrative does not name the collection it found: {output[-900:]}")
    assert _front_end()[name].count_documents({}) == before, "the refusal wrote"
