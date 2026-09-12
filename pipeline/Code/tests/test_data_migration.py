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

Four requests run as one sequence, each needing an operator click, because
the cases are about each other:

    1   the real migration
    2a  fired while the mutex is held   refused: one job at a time
    2b  fired once free, absent source  refused: nothing to move
    3   fired once free, target present refused: already there

Every click is a real one. The server refuses a decision arriving without the
mouse-click marker, so a pytest that could approve itself would be proving the
gate does not work.

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
import threading
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

    The runbook adjudicates. Every check below reads what it recorded.

    Every case runs. A case that fails is recorded and the sequence carries
    on, because the operator answers four approval pages to get here and
    stopping at the first failure spends three of those clicks on nothing --
    and hides whatever the later cases would have said. Every failure is
    reported together at the end.
    """
    migratable = seeded["migratable"]
    absent = seeded["absent_everywhere"]
    present = seeded["already_there"]
    failures: list[str] = []

    def check(passed: bool, message: str) -> bool:
        if not passed:
            _log.error("[FAIL] %s", message)
            failures.append(message)
        return passed

    # 1 -- the real one.
    began = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(migratable)
    check(fired.returncode == 0,
          f"1: the operator program failed: {(fired.stdout + fired.stderr)[-600:]}")

    record = _approval_record(migratable)
    if check(record is not None, "1: no approval record in Mongo"):
        check(record["approval"] is True, f"1: the record says approval={record.get('approval')!r}")
        check(record["human_click"] is True, f"1: the record carries no human click: {record.get('human_click')!r}")

    # The reservation appearing is the service saying the job has started.
    # The watcher runs alongside it: if the job dies instead of starting,
    # the wait ends there and says why, rather than running its full half
    # hour against a job that is already gone.
    held = False
    try:
        with _JobWatcher(since=began.isoformat()) as watcher:
            _await_the_service(held=True, every=1, watcher=watcher)
        held = True
    except Exception as exc:  # noqa: BLE001
        check(False, f"1: the service never became held: {exc}")

    # 2a -- while it is held. Only meaningful if it is: firing it against a
    # free service tests nothing, and says so rather than failing quietly.
    if held:
        _log.info("[case] 2a: a second invocation while the first still runs")
        second = datetime.datetime.now(datetime.timezone.utc)
        fired = _operator_run(absent)
        check(fired.returncode == 0,
              f"2a: the operator program failed: {(fired.stdout + fired.stderr)[-600:]}")
        status, exception = _the_job_ended(since=second)
        # Was it ever a second invocation? If the first released before the
        # second reached the reservation, the two never overlapped and the
        # requirement was not exercised. Saying "not met" there would be a
        # lie about the service: it would mean the pytest could not create
        # the condition, which is a fault in this file and not in the code
        # under test. The two are reported differently on purpose.
        said = _the_runbook_said(absent, since=second)
        took_it = any("reserved the service" in line for line in said)
        if took_it:
            check(False,
                  "2a: NOT DEMONSTRATED — the second job took the reservation, "
                  "so the service was free when it arrived and the two never "
                  "overlapped. It then abended for the wrong reason, which "
                  "says nothing about REQ-B-015: it says this pytest could "
                  "not hold the service long enough to test it.")
        else:
            check(status == "Failed",
                  f"2a: the second invocation did not abend, it ended {status!r}")
            check("mutex_held" in exception or "one job at a time" in exception,
                  f"2a: it abended, but not for being second: {exception[-400:]}")
    else:
        check(False, "2a: not attempted, the service was never held")

    # 2b -- once the first has finished, the same name for a different reason.
    try:
        _await_the_service(held=False, every=5)
    except Exception as exc:  # noqa: BLE001
        check(False, f"2b: the service never became free: {exc}")
    _log.info("[case] 2b: the same absent collection, now that it is free")
    third = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(absent)
    check(fired.returncode == 0,
          f"2b: the operator program failed: {(fired.stdout + fired.stderr)[-600:]}")
    status, exception = _the_job_ended(since=third)
    check(status == "Failed",
          f"2b: an absent source did not abend, it ended {status!r}")
    check("migration_source_absent" in exception,
          f"2b: it abended, but not for the source being absent: {exception[-400:]}")

    # 3 -- a collection already at the destination.
    try:
        _await_the_service(held=False, every=3)
    except Exception as exc:  # noqa: BLE001
        check(False, f"3: the service never became free: {exc}")
    _log.info("[case] 3: a collection already on the front end")
    fourth = datetime.datetime.now(datetime.timezone.utc)
    fired = _operator_run(present)
    check(fired.returncode == 0,
          f"3: the operator program failed: {(fired.stdout + fired.stderr)[-600:]}")
    status, exception = _the_job_ended(since=fourth)
    check(status == "Failed",
          f"3: a collection already there did not abend, it ended {status!r}")
    check("migration_target_exists" in exception,
          f"3: it abended, but not for the collection being there: {exception[-400:]}")

    # The first one landed whole, under the name it left with. Counted at
    # the destination, not read from what the runbook said about itself:
    # a migration that wrote nothing and logged a completion line would
    # satisfy the log and not the requirement.
    at_source = _source_db()[migratable].count_documents({})
    at_destination = _destination_db()[migratable].count_documents({})
    check(at_destination == at_source,
          f"1: the serving cluster holds {at_destination} documents in "
          f"{migratable!r}, the pipeline cluster holds {at_source}")

    # And it ended well. A migration that copies everything and then abends
    # is not a migration that worked: the operator reads a failed job, and
    # nothing downstream can tell that apart from one that wrote nothing.
    # The refusals abend on purpose; this one must not.
    status, exception = _the_job_ended(since=began)
    check(status == "Completed",
          f"1: the migration ended {status!r}, not 'Completed': "
          f"{exception[-400:]}")

    _log.info("[cases] %d of 4 cases reported a failure", len(failures))
    assert not failures, "\n".join(f"  - {f}" for f in failures)


# ── half one: the commit gate ────────────────────────────────────────────────

_BREAKAGES = {
    "REQ-B-001": (
        "destination = self._destination()",
        'destination = self._destination_database()[self._collection_name + "_migrated"]'),
    "REQ-B-003": (
        'if record.get("approval") is not True:',
        'if False and record.get("approval") is not True:'),
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




def _approval_record(collection: str) -> dict | None:
    """The decision as Mongo holds it. The log says what happened; this says
    what was recorded, and the record is what REQ-B-003 asks for."""
    # pipelineEditor, not frontendUser: the approval lives in pipelineAdmin
    # and the front-end identity has no rights there, by design.
    return ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _FRONT_END_CLUSTER
    )["pipelineAdmin"]["Authorizations"].find_one(
        {"collection": collection, "type": "data_migration"},
        sort=[("released_at", -1)])


_SERVICE_WAIT_SECONDS = 1800


def _source_db():
    """The pipeline cluster's published data, as the pipeline identity."""
    return ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _PIPELINE_CLUSTER)[_PIPELINE_DB]


def _destination_db():
    """The serving data, as the identity that serves it.

    frontendUser, because pipelineEditor has no rights on the serving
    database -- which is the boundary working, and the reason this check
    could not previously count what arrived.
    """
    return ChatHealthyMongoUtilities().getConnection(
        "frontendUser", _FRONT_END_CLUSTER)[_FRONT_END_DB]


def _mutex():
    from chathealthy_lib.mutex import MUTEX_COLLECTION, MUTEX_DATABASE
    return ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", _FRONT_END_CLUSTER
    )[MUTEX_DATABASE][MUTEX_COLLECTION]


_AUTOMATION_ACCOUNT = "PipelineToFrontEndPublicDataMigratorWorkManager"
_RESOURCE_GROUP = "rg-chathealthy-pipeline-dev"
_SUBSCRIPTION = "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee"


def _az(args: list[str], timeout: int = 90) -> str:
    return subprocess.run(["az", *args], capture_output=True, text=True,
                          shell=(sys.platform == "win32"),
                          timeout=timeout).stdout


class _JobWatcher:
    """Watches the service's jobs in a thread and remembers a failure.

    The mutex appearing is the only sign the waiter had that work started,
    so a job that died before taking it looked exactly like a job still
    starting -- and the wait ran its full half hour on a job that had been
    dead for four seconds. This asks Azure what became of the job, in
    parallel, so a failure ends the wait at once and brings the runbook's
    own exception with it.
    """

    def __init__(self, since: str) -> None:
        self._since = since
        self._stop = threading.Event()
        self.failure: str | None = None
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def __enter__(self) -> "_JobWatcher":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(4):
            raw = _az(["automation", "job", "list",
                       "--automation-account-name", _AUTOMATION_ACCOUNT,
                       "--resource-group", _RESOURCE_GROUP, "-o", "json"])
            try:
                jobs = [j for j in json.loads(raw or "[]")
                        if (j.get("creationTime") or "") > self._since]
            except ValueError:
                continue
            for job in jobs:
                if job.get("status") not in ("Failed", "Suspended", "Stopped"):
                    continue
                detail = _az(["rest", "--method", "get", "--url",
                              f"https://management.azure.com/subscriptions/{_SUBSCRIPTION}"
                              f"/resourceGroups/{_RESOURCE_GROUP}/providers/Microsoft.Automation"
                              f"/automationAccounts/{_AUTOMATION_ACCOUNT}/jobs/{job['name']}"
                              f"?api-version=2019-06-01"])
                try:
                    props = json.loads(detail or "{}").get("properties", {})
                except ValueError:
                    props = {}
                self.failure = (f"the job {job.get('status')}: "
                                f"{str(props.get('exception') or '')[-600:]}")
                return


def _await_the_service(held: bool, every: int,
                       watcher: "_JobWatcher | None" = None) -> dict:
    """Wait until the service is held, or until it is free. Returns the
    reservation when waiting for it to appear.

    The reservation appearing is the service saying a job has started, and
    it disappearing is the service saying that job is over. Neither is
    inferred from a timer here: the pytest reads what the service wrote.
    """
    wanted = "held" if held else "free"
    began = time.monotonic()
    while time.monotonic() - began < _SERVICE_WAIT_SECONDS:
        reservation = _mutex().find_one({})
        if (reservation is not None) == held:
            _log.info("[service] %s after %.0fs%s", wanted,
                      time.monotonic() - began,
                      f" — {reservation.get('holder')} has it for "
                      f"{reservation.get('about')!r}" if reservation else "")
            return reservation or {}
        if watcher is not None and watcher.failure:
            pytest.fail(f"the service never became {wanted} because "
                        f"{watcher.failure}")
        _log.info("[service] waiting for %s, asking again in %ds (%.0fs so far)",
                  wanted, every, time.monotonic() - began)
        time.sleep(every)
    pytest.fail(f"the service never became {wanted} within "
                f"{_SERVICE_WAIT_SECONDS}s")


def _the_job_ended(since, wait_seconds: int = 600) -> tuple[str, str]:
    """What became of the job the service started after `since`.

    Returns its terminal status and its exception text. A refusal is an
    abend: REQ-B-005, REQ-B-013 and REQ-B-015 all require the invocation to
    end abnormally rather than exit tidily reporting a problem, so the
    platform's own verdict on the job is the evidence, not a line the
    runbook chose to write about itself.
    """
    stamp = since.isoformat() if hasattr(since, "isoformat") else str(since)
    began = time.monotonic()
    while time.monotonic() - began < wait_seconds:
        raw = _az(["automation", "job", "list",
                   "--automation-account-name", _AUTOMATION_ACCOUNT,
                   "--resource-group", _RESOURCE_GROUP, "-o", "json"])
        try:
            jobs = sorted(
                (j for j in json.loads(raw or "[]")
                 if (j.get("creationTime") or "") > stamp),
                key=lambda j: j.get("creationTime") or "")
        except ValueError:
            jobs = []
        for job in jobs:
            if job.get("status") in ("Completed", "Failed", "Suspended", "Stopped"):
                detail = _az(["rest", "--method", "get", "--url",
                              f"https://management.azure.com/subscriptions/{_SUBSCRIPTION}"
                              f"/resourceGroups/{_RESOURCE_GROUP}/providers/Microsoft.Automation"
                              f"/automationAccounts/{_AUTOMATION_ACCOUNT}/jobs/{job['name']}"
                              f"?api-version=2019-06-01"])
                try:
                    props = json.loads(detail or "{}").get("properties", {})
                except ValueError:
                    props = {}
                return job["status"], str(props.get("exception") or "")
        _log.info("[job] no job has ended since %s yet, asking again", stamp)
        time.sleep(5)
    return "none", f"no job ended within {wait_seconds}s"


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
    from chathealthy_lib.mutex import give_back, take

    kind = f"pytest_mutex_{uuid.uuid4().hex[:8]}"
    take("pipelineEditor", _FRONT_END_CLUSTER, kind,
            holder="first", about="the first holder")
    try:
        with pytest.raises(ChatHealthyException) as refused:
            take("pipelineEditor", _FRONT_END_CLUSTER, kind,
                    holder="second", about="the second holder")
        assert refused.value.mode == "mutex_held", refused.value.mode
        assert "first" in str(refused.value), str(refused.value)
    finally:
        give_back("pipelineEditor", _FRONT_END_CLUSTER, kind)

    # Released, so the next holder takes it. A mutex that never comes back
    # refuses every job after the first and looks identical to one that works.
    take("pipelineEditor", _FRONT_END_CLUSTER, kind,
            holder="third", about="after the release")
    give_back("pipelineEditor", _FRONT_END_CLUSTER, kind)
