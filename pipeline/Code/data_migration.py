"""EPIC-010-F-108-S-001 — Data Migration.

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

import json
import os
import sys

from pymongo.errors import BulkWriteError

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

set_mongo_log_identity("dataMigrator")

_log = ChatHealthyLoggingService()

_BATCH = 1000


class MigratedCollection:
    """One collection, moved once, under its own name."""

    def __init__(self, collection_name: str) -> None:
        self._collection_name = collection_name

    @property
    def name(self) -> str:
        return self._collection_name

    def _source(self):
        return ChatHealthyMongoUtilities().getConnection(
            "dataMigrator", "ChatHealthyDataPipelines"
        )["PipelinePublicHealthData"][self._collection_name]

    def _destination(self):
        return ChatHealthyMongoUtilities().getConnection(
            "dataMigrator", "ChatHealthyFrontEnd"
        )["PublicHealthData"][self._collection_name]

    def _destination_database(self):
        return ChatHealthyMongoUtilities().getConnection(
            "dataMigrator", "ChatHealthyFrontEnd"
        )["PublicHealthData"]

    def _source_database(self):
        return ChatHealthyMongoUtilities().getConnection(
            "dataMigrator", "ChatHealthyDataPipelines"
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
        if not self._collection_name or not isinstance(self._collection_name, str):
            raise ChatHealthyException(
                mode="migration_collection_name_invalid",
                component="MigratedCollection",
                message="a collection name is required")

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


def _raise_unauthorized(collection: str, authorization: dict) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="migration_not_authorized",
        component="data_migration",
        message=(
            f"no human authorization on the payload for {collection!r}; a "
            f"migration runs only after an operator clicked APPROVE"),
        authorization=json.dumps(authorization)[:400])


def main() -> int:
    body = _webhook_body()
    collection = str(body.get("collection") or "")
    authorization = body.get("authorization") or {}
    released_at = body.get("released_at") or ""

    migrated = MigratedCollection(collection)

    # The release is recorded before the first document moves, so a migration
    # that ran is always preceded by the record of the click that released it.
    _log.info("data_migration released collection=%s verdict=%s human_click=%s "
              "released_at=%s",
              collection, authorization.get("verdict"),
              authorization.get("human_click"), released_at)

    try:
        if (authorization.get("verdict") != "approve"
                or authorization.get("human_click") is not True):
            _raise_unauthorized(collection, authorization)
        migrated.refuse_unless_migratable()
        expected = migrated.source_count()
    except ChatHealthyException as exc:
        _log.error("data_migration refused collection=%s mode=%s: %s",
                   collection, exc.mode, exc)
        return 1

    _log.info("data_migration start collection=%s expected=%d", collection, expected)

    # Constraints first, on an empty collection, so every duplicate is
    # refused by the insert that carries it rather than discovered afterwards.
    for name in migrated.build_constraint_indexes():
        _log.info("data_migration constraint_index collection=%s name=%s",
                  collection, name)

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
              "released_at=%s", collection, written, len(built), released_at)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
