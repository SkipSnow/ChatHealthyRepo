"""EPIC-010-F-108-S-001 — Data Migration.

Moves a collection from ChatHealthyDataPipelines.PipelinePublicHealthData to
ChatHealthyFrontEnd.PublicHealthData under the name it already has.

One class realizes this story. Its constructor takes the collection name and
nothing else, and nothing outside it assigns anything else to it. Every
collection the migration touches is reached through an instance of it.

The class raises and does not log; main() logs and does not raise. Rule-065
forbids a function that raises from also logging, and REQ-B-005 requires the
refusal to be both raised and logged -- the split satisfies both.
"""
from __future__ import annotations

import argparse
import sys

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

    def _source(self):
        return ChatHealthyMongoUtilities().getConnection(
            "dataMigrator", "ChatHealthyDataPipelines"
        )["PipelinePublicHealthData"][self._collection_name]

    def _target_database(self):
        return ChatHealthyMongoUtilities().getConnection(
            "dataMigrator", "ChatHealthyFrontEnd"
        )["PublicHealthData"]

    def exists_at_destination(self) -> bool:
        return self._collection_name in self._target_database().list_collection_names()

    def migrate(self) -> dict:
        """Copy every document across. Raises rather than touching a name that
        is already taken; the caller logs the refusal."""
        if not self._collection_name or not isinstance(self._collection_name, str):
            raise ChatHealthyException(
                mode="migration_collection_name_invalid",
                component="MigratedCollection",
                message="a collection name is required")

        if self.exists_at_destination():
            raise ChatHealthyException(
                mode="migration_target_exists",
                component="MigratedCollection",
                message=(
                    f"{self._collection_name!r} is already present in "
                    f"ChatHealthyFrontEnd.PublicHealthData; nothing was written"),
                collection=self._collection_name)

        source = self._source()
        target = self._target_database()[self._collection_name]
        read = written = 0
        batch: list[dict] = []
        for doc in source.find({}):
            batch.append(doc)
            read += 1
            if len(batch) >= _BATCH:
                target.insert_many(batch, ordered=False)
                written += len(batch)
                batch = []
        if batch:
            target.insert_many(batch, ordered=False)
            written += len(batch)

        return {
            "collection": self._collection_name,
            "read": read,
            "written": written,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate one collection to the front-end cluster.")
    parser.add_argument("collection")
    parser.add_argument(
        "--released-by", required=True,
        help="the human who signed off on this data; recorded on the result")
    args = parser.parse_args()

    try:
        result = MigratedCollection(args.collection).migrate()
    except ChatHealthyException as exc:
        _log.error("data_migration refused collection=%s mode=%s: %s",
                   args.collection, exc.mode, exc)
        return 1

    _log.info("data_migration complete collection=%s read=%d written=%d released_by=%s",
              result["collection"], result["read"], result["written"],
              args.released_by)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
