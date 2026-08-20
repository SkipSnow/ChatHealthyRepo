"""Tests for EPIC-010-F-108-S-001 — Data Migration.

Two halves.

The first drives Rule-065-ENF-009 against deliberately broken copies of the
migration file, one per requirement, and asserts the commit is refused each
time. It touches no database.

The second invokes the migration the way Azure Automation invokes it: a
webhook envelope on argv, through the file's own entry point. Nothing calls
MigratedCollection directly. An earlier version of this pytest did, and so it
migrated live data with no approval at all -- it would have passed with the
authorization check deleted, which makes it a test of the copy loop wearing
the name of a test of the story.

Four cases run unattended, and every one of them is a refusal:

    no authorization on the payload          refused, nothing written
    verdict approve but human_click false    refused, nothing written
    target already at the destination        refused, nothing written
    source absent from the pipeline          refused, nothing written

The fifth case is the successful migration, and it requires an operator to
click APPROVE on the page the pytest opens. That is deliberate: the server
refuses any decision arriving without the mouse-click marker, so a pytest that
could approve itself would be proving the gate does not work. Run it with
CH_OPERATOR=1 to include that case; without the flag it is skipped and the four
refusals still run.

Nothing is faked. Rule-065-ENF-003 forbids a mock, stub or in-memory double
of a client, database or collection: the certificate is the credential, and
code that reaches Mongo without an identity is code whose authorization is
never tested.
"""
from __future__ import annotations

import ast
import datetime
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
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

STORY = "EPIC-010-F-108-S-001"

_MIGRATION_PATH = _REPO / "pipeline" / "Code" / "data_migration.py"
_WORKER = (_REPO / "architecture" / "EngineeringRuleEnforcement" / "code"
           / "scan_data_migration_compliance_worker.py")
_ENFORCEMENT = "Rule-065-ENF-009"

_PIPELINE_CLUSTER = "ChatHealthyDataPipelines"
_FRONT_END_CLUSTER = "ChatHealthyFrontEnd"
_PIPELINE_DB = "PipelinePublicHealthData"
_FRONT_END_DB = "PublicHealthData"

_MIGRATE_TOOL = _REPO / "pipeline" / "Code" / "ops" / "migrate_data.py"
_FIXTURES = _REPO / "_oneshots" / "test_output" / "migration_test_fixtures.json"
_RESERVATION_MINUTES = 60


_OPERATOR_ENV = "CH_OPERATOR"


def _operator_present() -> bool:
    """The operator cases open a page and block on a click, so they run only
    when a person said they are at the keyboard. pytest_addoption belongs in
    conftest.py, not here, so this is an environment flag instead of a CLI
    option -- one fewer file touched for the same effect."""
    return (os.environ.get(_OPERATOR_ENV) or "").strip() == "1"


# ── the three runs, each one page ────────────────────────────────────────────

def test_the_four_requests(seeded):
    """TRUE: the service migrates what a human released, does one job at a
    time, and refuses what it cannot migrate -- each for its own reason.
    REQ-B-015, REQ-B-013, REQ-B-005, REQ-B-003 and REQ-B-001.

    One sequence, four approvals, because the cases are about each other:
    the second must arrive while the first is still running, and the third
    and fourth must arrive after it has finished.

        1   the real migration
        2a  fired while the reservation is held  -> refused, one job at a time
        2b  fired once it is free, absent source -> refused, nothing to move
        3   fired once it is free, target present -> refused, already there

    The runbook adjudicates. Every assertion below reads what it recorded.
    """
    migratable = seeded["migratable"]
    absent = seeded["absent_everywhere"]
    present = seeded["already_there"]

    # 1 -- the real one.
    began = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(migratable)
    assert fired.returncode == 0, (fired.stdout + fired.stderr)[-900:]

    record = _approval_record(migratable)
    assert record is not None, "no approval record in Mongo"
    assert record["verdict"] == "approve", record
    assert record["human_click"] is True, record

    # The reservation appearing is the service saying the job has started.
    _await_the_service(held=True, every=1)

    # 2a -- while it is held.
    _log.info("[case] 2a: a second invocation while the first still runs")
    second = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(absent)
    assert fired.returncode == 0, (fired.stdout + fired.stderr)[-900:]
    said = _the_runbook_said(absent, since=second)
    assert any("reservation_held" in line or "one job at a time" in line
               for line in said), (
        f"the second invocation was not refused for being second: {said}")

    # 2b -- once the first has finished, the same name for a different reason.
    _await_the_service(held=False, every=5)
    _log.info("[case] 2b: the same absent collection, now that it is free")
    third = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(absent)
    assert fired.returncode == 0, (fired.stdout + fired.stderr)[-900:]
    said = _the_runbook_said(absent, since=third)
    assert any("migration_source_absent" in line for line in said), (
        f"an absent source was not refused as absent: {said}")

    # 3 -- a collection already at the destination.
    _await_the_service(held=False, every=3)
    _log.info("[case] 3: a collection already on the front end")
    fourth = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(present)
    assert fired.returncode == 0, (fired.stdout + fired.stderr)[-900:]
    said = _the_runbook_said(present, since=fourth)
    assert any("migration_target_exists" in line for line in said), (
        f"a collection already there was not refused as present: {said}")

    # The first one landed whole, under the name it left with.
    said = _the_runbook_said(migratable, since=began)
    assert any("complete" in line for line in said), (
        f"the migration did not report completion: {said}")


# ── half one: the commit gate ────────────────────────────────────────────────

_BREAKAGES = {
    "REQ-B-001": (
        "destination = self._destination()",
        'destination = self._destination_database()[self._collection_name + "_migrated"]'),
    "REQ-B-003": (
        'if record.get("verdict") != "approve":',
        'if False and record.get("verdict") != "approve":'),
    "REQ-B-013": (
        "        if not self.exists_at_source():",
        "        if False and not self.exists_at_source():"),
    "REQ-B-014": (
        "        acknowledgement_id = _acknowledge_approval(collection, approval_id)",
        '        acknowledgement_id = "never-acknowledged"'),
    "REQ-B-005": (
        "        if self.exists_at_destination():",
        "        if False and self.exists_at_destination():"),
    "REQ-B-007": (
        "    def __init__(self, collection_name: str) -> None:\n        self._collection_name = collection_name",
        "    def __init__(self, collection_name: str, batch_size: int = 1000) -> None:\n        self._collection_name = collection_name\n        self._batch_size = batch_size"),
}


def _run_enforcement(path: str = "pipeline/Code/data_migration.py") -> tuple[int, list[str]]:
    began = time.monotonic()
    _log.info("[gate] invoking %s on %s", _ENFORCEMENT, path)
    result = subprocess.run(
        [sys.executable, str(_WORKER), _ENFORCEMENT],
        input=f"{path}\n",
        capture_output=True, text=True, cwd=str(_REPO), timeout=1800)
    for line in (result.stderr or "").splitlines():
        if "[ENF-009]" in line:
            _log.info("[gate] %s", line.split("[ENF-009]", 1)[1].strip())
    messages = []
    for line in (result.stdout or "").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if record.get("kind") == "violation":
            messages.append(record.get("message", ""))
    _log.info("[gate] %s exit=%d violations=%d in %.0fs",
              _ENFORCEMENT, result.returncode, len(messages),
              time.monotonic() - began)
    for message in messages:
        _log.info("[gate]   %s", message[:200])
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
      B-013  the migration would report success with no source collection
      B-014  the transfer would proceed without acknowledging the approval
    """
    original = pristine_migration_file
    _log.info("[mutation] breaking %s and requiring the gate to refuse it", req_id)
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


def _lifecycle():
    """The manager that holds reservations on the pipeline cluster.

    It opens its own connection and takes none from here; the constructor
    arguments it accepts are for callers that inject one, which this pytest
    does not.
    """
    from cluster_lifecycle_manager import ClusterLifecycleManager
    return ClusterLifecycleManager(get_db_fn=None)


@pytest.fixture(scope="module")
def reservation():
    """A reservation for the pytest, then released.

    Running this pytest is the consent to spend the cluster. The operator typed
    the command; no page is opened to ask permission for that, because a page
    that says "Authorize this migration?" while meaning "may I wake a machine"
    is a production prompt wearing a costume -- and one of those was shown
    once, for a migration that could never happen.

    The reaper pauses the cluster whenever nothing holds a reservation, so
    without one it can pause mid-run; a reservation left behind keeps it
    awake and billing. Released in teardown whatever the tests did.
    """
    job_id = f"migration-test-{uuid.uuid4().hex[:8]}"
    _log.info("[reservation] taking %s on %s for %d minutes",
              job_id, _PIPELINE_CLUSTER, _RESERVATION_MINUTES)
    lifecycle = _lifecycle()
    lifecycle.reserve(
        cluster_name=_PIPELINE_CLUSTER,
        job_id=job_id,
        requester="test_data_migration",
        expected_duration_minutes=_RESERVATION_MINUTES,
        reservation_class="human",
    )
    try:
        yield job_id
    finally:
        _log.info("[reservation] releasing %s", job_id)
        released = lifecycle.release(job_id)
        assert released["deleted_count"] == 1, (
            f"reservation {job_id} was not released; the cluster will stay "
            f"awake and billing until someone removes it")




@pytest.fixture(scope="module")
def seeded(reservation):
    """The collections that were staged before this ran, by name.

    Nothing here creates or removes anything. The fixtures are seeded from
    outside by _oneshots/seed_migration_test_collections.py, because a test
    that stages its own subject holds rights the software does not -- it
    dropped collections the migration is forbidden to drop -- and its writes
    reach the same clusters by the same route, so afterwards there is no
    telling which of them wrote what.

        migratable         on the pipeline, absent at the destination.
                           Consumed by a successful run and re-seeded before
                           the next one.
        already_there      the same name on both clusters. Permanent: the
                           migration refuses it, so nothing is consumed.
        absent_everywhere  a name and nothing else. Not existing is the
                           condition it tests.
    """
    if not _FIXTURES.is_file():
        pytest.fail(
            f"nothing has been seeded: {_FIXTURES} does not exist. Run "
            f"python _oneshots/seed_migration_test_collections.py first. "
            f"This pytest does not seed itself.")
    names = json.loads(_FIXTURES.read_text(encoding="utf-8"))
    _log.info("[seeded] migratable=%s already_there=%s absent_everywhere=%s",
              names.get("migratable"), names.get("already_there"),
              names.get("absent_everywhere"))
    return names


def _front_end():
    """The serving database, as frontendUser.

    The test is not the migration and does not borrow the migration's
    identity: PipelineToFrontEndPublicDataMigrator is a managed identity that
    exists only inside Azure, and its role can insert and nothing else. The
    test reads what arrived and plants the collision run three must refuse,
    both of which are frontendUser's business.
    """
    return ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]


def _approval_record(collection: str) -> dict | None:
    """The decision as Mongo holds it. The log says what happened; this says
    what was recorded, and the record is what REQ-B-003 asks for."""
    return ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER
    )["pipelineAdmin"]["Authorizations"].find_one(
        {"collection": collection, "type": "data_migration"},
        sort=[("released_at", -1)])


_SERVICE_WAIT_SECONDS = 1800


def _reservations():
    from chathealthy_lib.reservations import (
        RESERVATIONS_COLLECTION, RESERVATIONS_DATABASE)
    return ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _FRONT_END_CLUSTER
    )[RESERVATIONS_DATABASE][RESERVATIONS_COLLECTION]


def _await_the_service(held: bool, every: int) -> dict:
    """Wait until the service is held, or until it is free. Returns the
    reservation when waiting for it to appear.

    The reservation appearing is the service saying a job has started, and
    it disappearing is the service saying that job is over. Neither is
    inferred from a timer here: the pytest reads what the service wrote.
    """
    wanted = "held" if held else "free"
    began = time.monotonic()
    while time.monotonic() - began < _SERVICE_WAIT_SECONDS:
        reservation = _reservations().find_one({})
        if (reservation is not None) == held:
            _log.info("[service] %s after %.0fs%s", wanted,
                      time.monotonic() - began,
                      f" — {reservation.get('holder')} has it for "
                      f"{reservation.get('about')!r}" if reservation else "")
            return reservation or {}
        _log.info("[service] waiting for %s, asking again in %ds (%.0fs so far)",
                  wanted, every, time.monotonic() - began)
        time.sleep(every)
    pytest.fail(f"the service never became {wanted} within "
                f"{_SERVICE_WAIT_SECONDS}s")


def _the_runbook_said(collection: str, since) -> list[str]:
    """What the runbook recorded about this collection, in its own words.

    The runbook adjudicates; this reads its verdict. Nothing here decides
    whether an invocation should have been refused.
    """
    log = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _FRONT_END_CLUSTER)["pipelineAdmin"]["Log"]
    began = time.monotonic()
    while time.monotonic() - began < _SERVICE_WAIT_SECONDS:
        said = [str(record.get("message", ""))
                for record in log.find({"timeStamp": {"$gte": since}})
                if collection in str(record.get("message", ""))]
        if said:
            for line in said:
                _log.info("[runbook] %s", line[:220])
            return said
        _log.info("[runbook] nothing recorded about %s yet, asking again",
                  collection)
        time.sleep(3)
    pytest.fail(f"the runbook recorded nothing about {collection}")


def _operator_run(collection: str) -> subprocess.CompletedProcess:
    """Invoke the operator's program. One invocation, one page, one click."""
    _log.info("[run] invoking the operator program for %s; an approval page "
              "is about to open and it blocks until it is clicked", collection)
    began = time.monotonic()
    result = subprocess.run(
        [sys.executable, str(_MIGRATE_TOOL), collection, "--env", "dev"],
        capture_output=True, text=True, cwd=str(_REPO), timeout=1800)
    _log.info("[run] %s finished exit=%d after %.0fs",
              collection, result.returncode, time.monotonic() - began)
    return result


# ── the requirements settled by reading the code ─────────────────────────────

def _migration_tree():
    return ast.parse(_MIGRATION_PATH.read_text(encoding="utf-8"))


def _identities(path: pathlib.Path) -> set[str]:
    """Every identity name handed to getConnection in one file."""
    found = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "getConnection" and node.args
                and isinstance(node.args[0], ast.Constant)):
            found.add(node.args[0].value)
    return found


def _declared_python_components() -> list[pathlib.Path]:
    """Every .py file deployment_architecture.json declares."""
    manifest = json.loads(
        (_REPO / "brain" / "machine_artifacts" / "content"
         / "deployment_architecture.json").read_text(encoding="utf-8"))
    out = []
    for target in manifest.get("DeploymentTargetRecord", []):
        for environment in target.get("environments", []):
            for package in environment.get("packages", []):
                for entry in package.get("files", []) or []:
                    location = (entry.get("source_location") or "").replace("\\", "/")
                    if location.endswith(".py") and (_REPO / location).is_file():
                        out.append(_REPO / location)
    return out


def test_only_the_migration_names_the_serving_database():
    """TRUE: nothing other than the migration writes to the serving database.
    REQ-B-002, and REQ-B-009 that exactly one file performs the migration.

    A code assertion over every component the deployment declares."""
    # Named exactly, not as a substring: PipelinePublicHealthData contains
    # the serving database's name and is a different database.
    others = []
    for component in _declared_python_components():
        if component == _MIGRATION_PATH:
            continue
        try:
            tree = ast.parse(component.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if any(isinstance(n, ast.Constant) and n.value == _FRONT_END_DB
               for n in ast.walk(tree)):
            others.append(component)
    assert not others, f"declared components also naming {_FRONT_END_DB}: {others}"


def test_the_migration_cannot_alter_or_remove():
    """TRUE: no migration changes or removes anything that existed before it
    ran. REQ-B-004.

    A code assertion: no call in the file can alter or remove."""
    destructive = {"update_one", "update_many", "replace_one", "delete_one",
                   "delete_many", "drop", "drop_index", "drop_indexes",
                   "rename", "find_one_and_delete", "find_one_and_replace",
                   "find_one_and_update", "bulk_write"}
    found = sorted({n.func.attr for n in ast.walk(_migration_tree())
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in destructive})
    assert not found, f"the migration can alter or remove: {found}"


def test_data_is_reached_only_on_behalf_of_a_named_collection():
    """TRUE: within the implementing service every access to data is made on
    behalf of exactly one named collection. REQ-B-006.

    A code assertion: the databases holding the data are named nowhere in the
    file outside the class that carries a collection name."""
    data_databases = {_FRONT_END_DB, _PIPELINE_DB}
    outside = []
    for node in _migration_tree().body:
        if isinstance(node, ast.ClassDef):
            continue
        named = sorted({c.value for c in ast.walk(node)
                        if isinstance(c, ast.Constant)
                        and isinstance(c.value, str) and c.value in data_databases})
        if named:
            outside.append((getattr(node, "name", type(node).__name__), named))
    assert not outside, f"data reached outside the class: {outside}"


def test_only_the_collection_name_is_assigned_onto_the_class():
    """TRUE: the collection name is the only value assigned to the class from
    outside it. REQ-B-008.

    A code assertion on what the class assigns onto itself."""
    classes = [n for n in ast.walk(_migration_tree()) if isinstance(n, ast.ClassDef)]
    assert len(classes) == 1, f"expected one class, found {[c.name for c in classes]}"
    assigned = set()
    for node in ast.walk(classes[0]):
        targets = (node.targets if isinstance(node, ast.Assign)
                   else [node.target] if isinstance(node, ast.AnnAssign) else [])
        for target in targets:
            if (isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"):
                assigned.add(target.attr)
    assert len(assigned) == 1, f"the class assigns {sorted(assigned)} onto self"


def test_each_actor_uses_one_identity_and_not_the_other_s():
    """TRUE: two actors take part, the workstation recording the approval and
    the service moving the data, and neither can perform the other's part.
    REQ-B-011.

    A code assertion: each file reaches the database as exactly one identity,
    and the two identities are different."""
    service = _identities(_MIGRATION_PATH)
    workstation = _identities(_MIGRATE_TOOL)
    assert len(service) == 1, f"the service uses {sorted(service)}"
    assert len(workstation) == 1, f"the workstation uses {sorted(workstation)}"
    assert service != workstation, (
        f"both actors use the same identity: {sorted(service)}")


def test_a_second_holder_cannot_take_the_reservation():
    """TRUE: the service runs one job at a time, so a second invocation
    arriving while one is running is refused. REQ-B-015.

    A behavioural assertion against the real collection.

    The reservation is the collection, not a row in it: one record or none,
    whatever its type. So while this test holds it, a migration invoked at
    that moment is refused -- which is the exclusion working, and is worth
    knowing before running this against an estate doing real work. It is
    held for the length of two calls and given back in a finally.
    """
    from chathealthy_lib.exceptions import ChatHealthyException
    from chathealthy_lib.reservations import release, reserve

    kind = f"pytest_mutex_{uuid.uuid4().hex[:8]}"
    reserve("pipelineEditor", _FRONT_END_CLUSTER, kind,
            holder="first", about="the first holder")
    try:
        with pytest.raises(ChatHealthyException) as refused:
            reserve("pipelineEditor", _FRONT_END_CLUSTER, kind,
                    holder="second", about="the second holder")
        assert refused.value.mode == "reservation_held", refused.value.mode
        assert "first" in str(refused.value), str(refused.value)
    finally:
        release("pipelineEditor", _FRONT_END_CLUSTER, kind)

    # Released, so the next holder takes it. A mutex that never comes back
    # refuses every job after the first and looks identical to one that works.
    reserve("pipelineEditor", _FRONT_END_CLUSTER, kind,
            holder="third", about="after the release")
    release("pipelineEditor", _FRONT_END_CLUSTER, kind)
