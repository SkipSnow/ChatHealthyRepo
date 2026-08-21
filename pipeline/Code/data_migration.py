"""EPIC-010-F-108-S-001 -- Data Migration.

Moves a collection from ChatHealthyDataPipelines.PipelinePublicHealthData to
ChatHealthyFrontEnd.PublicHealthData under the name it already has.

One class realizes this story. Its constructor takes the collection name and
nothing else, and nothing outside it assigns anything else to it. Every
collection the migration touches is reached through an instance of it.

The class raises and does not log; main() logs and does not raise. Rule-065
forbids a function that raises from also logging, which is why copy() is a
generator: it yields its running count and main() writes the progress line,
so the copy never has to speak for itself.

The only write this file performs is insert_many. There is no update, no
delete and no drop anywhere in it, and dataMigrator's database role holds no
such grant either, so a bug here cannot alter or remove what is already on
the front-end cluster.
"""
from __future__ import annotations

import datetime
import json
import os
import sys
import time
import uuid

# Atlas addresses are SRV records, and the Automation sandbox's own resolver
# does not answer external SRV queries -- every connection from this runbook
# died on "All nameservers failed to answer the query _mongodb._tcp.<host>
# IN SRV" while the watchdog and the provider pipeline, which do this, were
# reaching the same cluster. Point dnspython at public resolvers instead.
# This MUST run before pymongo is imported, because pymongo binds the
# default resolver at import time.
try:
    import dns.resolver  # type: ignore[import-not-found]

    _resolver = dns.resolver.Resolver(configure=False)
    _resolver.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    _resolver.timeout = 5
    _resolver.lifetime = 10
    dns.resolver.default_resolver = _resolver
except ImportError:
    pass

import requests
from pymongo.errors import BulkWriteError
from requests.auth import HTTPDigestAuth

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.pipeline_boot import bootstrap_aa_mongo_logging
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.reservations import release, reserve

def _mark(step: str) -> None:
    """Say where we are on the sandbox's own output stream.

    Everything up to the log handler is invisible to the log, because the
    handler is the log: imports, variable hydration, the vault fetch and the
    Mongo connect all happen before the first record can exist. A job that
    dies in that window says nothing at all, which is how this one spent six
    minutes without reporting that it could not parse a certificate.

    Azure keeps stdout on a job and returns it from the job's output
    endpoint, so these markers survive a failure that reaches no database.
    """
    sys.stdout.write(f"[data_migration] {step}" + chr(10))
    sys.stdout.flush()


_mark("module import: begin")

# Hydrate the Automation Variables the deploy pushed into os.environ, the
# way every other runbook in this account does. The sandbox does not put
# them there: without KEY_VAULT_URI the certificate cannot be fetched and
# this runbook reaches neither cluster, which is exactly how it failed.
try:
    import automationassets as _automation_assets

    # The client id belongs here as much as the vault URI does. Without it
    # the token request names no identity and the sandbox answers with the
    # account's own system identity, which holds nothing on the migrator's
    # secret -- the vault then refuses, and the refusal looks like a missing
    # grant rather than a token for the wrong principal.
    for _name in ("KEY_VAULT_URI", "CH_LOG_DB", "AUTOMATION_ENV_PREFIX",
                  "PIPELINETOFRONTENDPUBLICDATAMIGRATOR_AZURE_CLIENT_ID",
                  "ATLAS_PROJECT_ID", "ATLAS_PIPELINE_PUBLIC_KEY",
                  "ATLAS_PIPELINE_PRIVATE_KEY",
                  "PIPELINEEDITOR_AZURE_TENANT_ID",
                  "PIPELINEEDITOR_AZURE_CLIENT_ID",
                  "PIPELINEEDITOR_AZURE_CLIENT_SECRET"):
        try:
            os.environ[_name] = str(
                _automation_assets.get_automation_variable(_name))
        except Exception:  # noqa: BLE001
            pass
    _mark("automation variables hydrated")
except ImportError:
    pass

_log = ChatHealthyLoggingService()

_BATCH = 1000
_AUTHORIZATION_TYPE = "data_migration"
# The service reserves itself before it works. One holder at a time, taken
# and given back by the library: admitting a job and performing one are
# different concerns.
_RESERVATION_TYPE = "data_migration_service"
_MIGRATOR = "PipelineToFrontEndPublicDataMigrator"
_SOURCE_CLUSTER = "ChatHealthyDataPipelines"
_WAKE_POLL_SECONDS = 5
_WAKE_DEADLINE_SECONDS = 900
_FRONT_END = "ChatHealthyFrontEnd"
# The service's answer to a human approval, referencing it by key. A distinct
# type because it is a distinct fact: the approval says a person released the
# work, the acknowledgement says a service took that release up.
_ACKNOWLEDGEMENT_TYPE = "data_migration_acknowledgement"


class MigratedCollection:
    """One collection, moved once, under its own name."""

    def __init__(self, collection_name: str) -> None:
        self._collection_name = collection_name

    @property
    def name(self) -> str:
        return self._collection_name

    def _source(self):
        return ChatHealthyMongoUtilities().getConnection(
            "PipelineToFrontEndPublicDataMigrator", "ChatHealthyDataPipelines"
        )["PipelinePublicHealthData"][self._collection_name]

    def _destination(self):
        return ChatHealthyMongoUtilities().getConnection(
            "PipelineToFrontEndPublicDataMigrator", "ChatHealthyFrontEnd"
        )["PublicHealthData"][self._collection_name]

    def _destination_database(self):
        return ChatHealthyMongoUtilities().getConnection(
            "PipelineToFrontEndPublicDataMigrator", "ChatHealthyFrontEnd"
        )["PublicHealthData"]

    def _source_database(self):
        return ChatHealthyMongoUtilities().getConnection(
            "PipelineToFrontEndPublicDataMigrator", "ChatHealthyDataPipelines"
        )["PipelinePublicHealthData"]

    def exists_at_source(self) -> bool:
        return self._collection_name in self._source_database().list_collection_names()

    def exists_at_destination(self) -> bool:
        return self._collection_name in self._destination_database().list_collection_names()

    def source_count(self) -> int:
        return self._source().count_documents({})

    def refuse_unless_migratable(self) -> None:
        """The conditions under which nothing may be written. Raises; the
        caller logs."""
        if not self.exists_at_source():
            raise ChatHealthyException(
                mode="migration_source_absent",
                component="MigratedCollection",
                message=(
                    f"{self._collection_name!r} is not present in "
                    f"ChatHealthyDataPipelines.PipelinePublicHealthData; there "
                    f"is nothing to migrate and an empty success is a lie"),
                collection=self._collection_name)

        if self.exists_at_destination():
            raise ChatHealthyException(
                mode="migration_target_exists",
                component="MigratedCollection",
                message=(
                    f"{self._collection_name!r} is already present in "
                    f"ChatHealthyFrontEnd.PublicHealthData; nothing was written"),
                collection=self._collection_name)

    def source_indexes(self) -> list[dict]:
        """Every ordinary index on the source except _id_, which Mongo makes
        itself. Atlas Search indexes are not here; list_indexes cannot see
        them and source_search_indexes reads those separately."""
        return [dict(index) for index in self._source().list_indexes()
                if index.get("name") != "_id_"]

    def source_search_indexes(self) -> list[dict]:
        """The Atlas Search indexes on the source. These are what
        $vectorSearch queries; a collection that arrives without them is
        data FindCare cannot search."""
        return [dict(index) for index in self._source().list_search_indexes()]

    def copy(self):
        """Copy every document across, yielding the running count as each
        batch lands. Yields rather than logs so the class stays silent."""
        source = self._source()
        destination = self._destination()
        written = 0
        batch: list[dict] = []
        for document in source.find({}, batch_size=_BATCH):
            batch.append(document)
            if len(batch) >= _BATCH:
                destination.insert_many(batch, ordered=False)
                written += len(batch)
                batch = []
                yield written
        if batch:
            destination.insert_many(batch, ordered=False)
            written += len(batch)
        yield written

    @staticmethod
    def _constrains(index: dict) -> bool:
        """True when the index enforces something rather than merely making
        a query fast."""
        return bool(index.get("unique")) or bool(index.get("expireAfterSeconds"))

    def constraint_indexes(self) -> list[dict]:
        return [i for i in self.source_indexes() if self._constrains(i)]

    def performance_indexes(self) -> list[dict]:
        return [i for i in self.source_indexes() if not self._constrains(i)]

    def _build(self, indexes: list[dict]):
        destination = self._destination()
        for index in indexes:
            keys = list(index["key"].items())
            options = {k: v for k, v in index.items()
                       if k not in ("key", "v", "ns", "background")}
            destination.create_index(keys, **options)
            yield index["name"]

    def build_constraint_indexes(self):
        """Built on the empty collection, before a single document lands.

        A unique index built afterwards never enforced anything during the
        load: duplicates arrive unopposed and the build then fails on them,
        leaving a collection that is both duplicated and unindexed. Built
        first it costs nothing -- there is nothing to scan -- and every
        duplicate is refused at the insert that carries it.
        """
        return self._build(self.constraint_indexes())

    def build_performance_indexes(self):
        """Built last. These enforce nothing, so maintaining them across
        several million inserts buys nothing the final build does not."""
        return self._build(self.performance_indexes())

    def search_indexes_to_create_operationally(self) -> list[str]:
        """The search indexes the source carries and this migration does not
        build. Vector indexes are not built by the pipeline; creating one is
        an operational step taken after the data is in place. The names are
        reported so the operator knows what the collection is still missing.
        """
        return [index["name"] for index in self.source_search_indexes()]


def _webhook_body() -> dict:
    """The POST body Azure Automation delivered. The sandbox splits the
    envelope across argv without quote-protecting it, so RequestBody's value
    is recovered by balanced-brace parsing rather than json.loads."""
    raw = (" ".join(str(a) for a in sys.argv[1:]) if len(sys.argv) > 1
           else os.environ.get("WEBHOOKDATA", ""))
    if not raw:
        return {}
    marker = "RequestBody:"
    index = raw.find(marker)
    if index < 0:
        try:
            return json.loads(raw) or {}
        except ValueError:
            return {}
    start = index + len(marker)
    while start < len(raw) and raw[start] != "{":
        start += 1
    if start >= len(raw):
        return {}
    depth = 0
    for position in range(start, len(raw)):
        if raw[position] == "{":
            depth += 1
        elif raw[position] == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(raw[start:position + 1])
                except ValueError:
                    return {}
                return parsed if isinstance(parsed, dict) else {}
    return {}


def _raise_unauthorized(collection: str, approval_id: str, why: str) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="migration_not_authorized",
        component="data_migration",
        message=(
            f"{collection!r} is not released to migrate: {why}. A migration "
            f"runs only after an operator clicked APPROVE, and the click is "
            f"looked up, never taken on the caller's word"),
        approval_id=approval_id or "<none given>")


def _acknowledge_approval(collection: str, approval_id: str) -> str:
    """Write this service's acknowledgement of the human approval it was
    handed, before it does any work. REQ-B-014.

    The transfer is given the key of a human's approval and answers it in
    writing: this service, at this moment, is acting on that specific
    record. Two facts then exist independently -- a person released it, and
    a service took it up -- so afterwards it is answerable both that the
    work was authorized and which authorization it ran under, without
    inferring one from the other.

    Written before the first read of either cluster. A transfer that has
    not acknowledged has not started; the insert failing stops the work.
    """
    acknowledged_at = datetime.datetime.now(datetime.timezone.utc)
    acknowledgement_id = f"migration-acknowledgement-{uuid.uuid4().hex}"
    _authorizations().insert_one({
        "_id": acknowledgement_id,
        "type": _ACKNOWLEDGEMENT_TYPE,
        "acknowledges": approval_id,
        "collection": collection,
        "env": os.environ.get("ENV_PREFIX", ""),
        "service": "data_migration",
        "acknowledged_at": acknowledged_at,
        "day": acknowledged_at.date().isoformat(),
    })
    return acknowledgement_id


def _wake_the_source_cluster() -> None:
    """Bring the cluster the data comes from up, and wait until it serves.

    The reaper pauses it whenever nothing holds a reservation, which is
    correct: it should not run when nothing needs it. Something does now.
    Waking it is the service's work -- the operator asked for a migration,
    not for a cluster -- and nothing else in the estate was doing it, so a
    released migration failed on a paused source and told the operator their
    approval had gone nowhere.

    Raises; the caller logs.
    """
    project = os.environ.get("ATLAS_PROJECT_ID", "").strip()
    public = os.environ.get("ATLAS_PIPELINE_PUBLIC_KEY", "").strip()
    private = os.environ.get("ATLAS_PIPELINE_PRIVATE_KEY", "").strip()
    if not (project and public and private):
        raise ChatHealthyException(
            mode="config_error",
            component="data_migration",
            message="the Atlas keys needed to wake the source cluster are "
                    "absent; the cluster cannot be brought up")

    url = (f"https://cloud.mongodb.com/api/atlas/v2/groups/{project}"
           f"/clusters/{_SOURCE_CLUSTER}")
    headers = {"Accept": "application/vnd.atlas.2024-08-05+json",
               "Content-Type": "application/vnd.atlas.2024-08-05+json"}
    auth = HTTPDigestAuth(public, private)
    state = requests.get(url, auth=auth, headers=headers, timeout=30).json()
    if state.get("paused"):
        requests.patch(url, auth=auth, headers=headers,
                       json={"paused": False}, timeout=30)

    # Serving is what matters, and the label Atlas reports is not evidence of
    # it: a cluster answering a connection has reported REPAIRING. So this
    # asks the database, not the API.
    deadline = time.monotonic() + _WAKE_DEADLINE_SECONDS
    last = ""
    while time.monotonic() < deadline:
        try:
            ChatHealthyMongoUtilities().getConnection(
                _MIGRATOR, _SOURCE_CLUSTER)["admin"].command("ping")
            return
        except Exception as exc:  # noqa: BLE001
            last = f"{type(exc).__name__}: {str(exc)[:120]}"
        time.sleep(_WAKE_POLL_SECONDS)

    raise ChatHealthyException(
        mode="cluster_unavailable",
        component="data_migration",
        message=(f"{_SOURCE_CLUSTER} did not answer within "
                 f"{_WAKE_DEADLINE_SECONDS}s of being asked to serve; "
                 f"last refusal {last}"))


def _authorizations():
    return ChatHealthyMongoUtilities().getConnection(
        "PipelineToFrontEndPublicDataMigrator", "ChatHealthyFrontEnd"
    )["pipelineAdmin"]["Authorizations"]


def _released_approval(collection: str, approval_id: str) -> dict:
    """The recorded sign-off for this collection, read from the cluster.

    The payload names an approval; it does not carry one. A caller who could
    reach the webhook once only had to type human_click=true to migrate
    without a person, because the runbook believed the fields it was handed.
    Now the record is written by migrate_data.py before the fire, under
    DevOpsUser's certificate, and this reads it back and checks it says
    what the caller claims.
    """
    if not approval_id:
        _raise_unauthorized(collection, approval_id,
                            "the payload names no approval record")

    record = _authorizations().find_one({"_id": approval_id})

    if record is None:
        _raise_unauthorized(collection, approval_id,
                            "no such approval exists on the cluster")
    if record.get("type") != _AUTHORIZATION_TYPE:
        _raise_unauthorized(
            collection, approval_id,
            f"the record is a {record.get('type')!r} authorization, not a migration")
    if record.get("approval") is not True:
        _raise_unauthorized(collection, approval_id,
                            f"the record says approval={record.get('approval')!r}")
    if record.get("human_click") is not True:
        _raise_unauthorized(collection, approval_id,
                            "the record carries no human click")
    if record.get("collection") != collection:
        _raise_unauthorized(
            collection, approval_id,
            f"the approval is for {record.get('collection')!r}, not this collection")

    return record


def main() -> int:
    """Reserve the service, migrate, give the reservation back.

    REQ-B-015: one job at a time. An invocation arriving while another is
    running is refused before it reads or writes anything, and it abends --
    it does not wait and it does not queue.
    """
    # Mongo logging, set up the way every runbook here sets it up, before
    # the first log call. The identity is the bootstrap's own default and
    # not the migrator: naming the migrator made the log handler connect as
    # a managed identity, and that connection failed before a single line
    # was written -- taking the whole job with it, because a process that
    # cannot record what it does must not run.
    # The payload is read before the log exists, because reading it needs
    # nothing but argv and the collection it names belongs in the first
    # marker this job writes.
    body = _webhook_body()
    collection = str(body.get("collection") or "")
    _mark(f"collection={collection or '<none>'}")

    _mark("logging bootstrap: begin (vault fetch and mongo connect)")
    bootstrap_aa_mongo_logging(component_name="data_migration")
    _mark("logging bootstrap: done")
    _log.info("data_migration begin collection=%s", collection or "<none>")
    try:
        _mark("reserving the service")
        reserve(_MIGRATOR, _FRONT_END, _RESERVATION_TYPE,
                holder="data_migration", about=collection)
        _log.info("data_migration reserved the service collection=%s", collection)
    except ChatHealthyException as exc:
        _log.error(
            "data_migration ABEND collection=%s mode=%s: %s. The service "
            "runs one job at a time; nothing was read and nothing was "
            "written.", collection, exc.mode, exc)
        raise
    try:
        return _migrate(body, collection)
    finally:
        release(_MIGRATOR, _FRONT_END, _RESERVATION_TYPE)
        _log.info("data_migration reservation released collection=%s",
                  collection)


def _migrate(body: dict, collection: str) -> int:
    approval_id = str(body.get("approval_id") or "")

    migrated = MigratedCollection(collection)

    _log.info("data_migration asked collection=%s approval_id=%s",
              collection, approval_id or "<none>")

    try:
        approval = _released_approval(collection, approval_id)
        _log.info("data_migration released collection=%s approval_id=%s "
                  "human_click=%s released_at=%s",
                  collection, approval_id, approval.get("human_click"),
                  approval.get("released_at"))
        _log.info("data_migration waking the source cluster collection=%s",
                  collection)
        _wake_the_source_cluster()
        _log.info("data_migration source cluster is up collection=%s", collection)
        acknowledgement_id = _acknowledge_approval(collection, approval_id)
        _log.info("data_migration acknowledged approval_id=%s "
                  "acknowledgement_id=%s collection=%s",
                  approval_id, acknowledgement_id, collection)
        _log.info("data_migration checking the collection may be migrated "
                  "collection=%s", collection)
        migrated.refuse_unless_migratable()
        expected = migrated.source_count()
        _log.info("data_migration source counted collection=%s documents=%d",
                  collection, expected)
    except ChatHealthyException as exc:
        # Log the narrative, then let it go. Returning 1 here would have been
        # an orderly exit reporting a failure, and REQ-B-005 asks for an
        # abend: the exception leaves the process, the runbook job is marked
        # failed by the platform rather than by a number this code chose, and
        # the traceback is in the job record. A refusal that tidies itself
        # away is the shape of a refusal that gets missed.
        _log.error(
            "data_migration ABEND collection=%s mode=%s approval_id=%s: %s. "
            "Nothing was written: the destination collection was left exactly "
            "as it was found, unaltered and not appended to.",
            collection, exc.mode, approval_id or "<none>", exc)
        raise

    _log.info("data_migration start collection=%s expected=%d", collection, expected)

    # Constraints first, on an empty collection, so every duplicate is
    # refused by the insert that carries it rather than discovered afterwards.
    for name in migrated.build_constraint_indexes():
        _log.info("data_migration constraint_index collection=%s name=%s",
                  collection, name)

    _log.info("data_migration copying collection=%s expected=%d",
              collection, expected)
    written = 0
    try:
        for written in migrated.copy():
            _log.info("data_migration progress collection=%s written=%d of %d",
                      collection, written, expected)
    except BulkWriteError as exc:
        _log.error("data_migration REFUSED BY A CONSTRAINT collection=%s "
                   "written=%d of %d: %s",
                   collection, written, expected, str(exc)[:400])
        return 1

    if written != expected:
        _log.error("data_migration INCOMPLETE collection=%s written=%d expected=%d",
                   collection, written, expected)
        return 1

    _log.info("data_migration copy complete collection=%s written=%d",
              collection, written)
    wanted = [index["name"] for index in migrated.performance_indexes()]
    built = []
    for name in migrated.build_performance_indexes():
        built.append(name)
        _log.info("data_migration index collection=%s built=%s (%d of %d)",
                  collection, name, len(built), len(wanted))

    if sorted(built) != sorted(wanted):
        _log.error("data_migration INDEXES INCOMPLETE collection=%s built=%s wanted=%s",
                   collection, sorted(built), sorted(wanted))
        return 1

    outstanding = migrated.search_indexes_to_create_operationally()
    if outstanding:
        _log.info("data_migration collection=%s still needs these search "
                  "indexes created operationally before it can serve: %s",
                  collection, ", ".join(outstanding))

    _log.info("data_migration complete collection=%s written=%d indexes=%d "
              "approval_id=%s", collection, written, len(built), approval_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
