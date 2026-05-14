"""Schema-validated writer for `deployment_architecture.json`.

Writes the brain artifact with deterministic ordering so the on-disk
bytes are reproducible across runs (operator review + diff-friendly).

Fetches the schema via HTTPS (no local fallback) and validates every
record before serialization. Validation failure raises before any bytes
hit disk.
"""
from __future__ import annotations

import json
from pathlib import Path

from record_loader import SCHEMA_URL, RecordLoader
from target_record import DeploymentCollection, TargetRecord

ENV_CANONICAL_ORDER: tuple[str, ...] = ("local", "dev", "qa", "prod")


class RecordWriter:
    """Schema-validates then writes records to disk.

    Construction fetches the schema via the same URL the loader uses,
    so reader and writer agree on the contract.
    """

    def __init__(self, schema_url: str = SCHEMA_URL) -> None:
        self._loader = RecordLoader(schema_url=schema_url)

    @staticmethod
    def _canonical_record(record: TargetRecord) -> dict[str, object]:
        env_rank = {e: i for i, e in enumerate(ENV_CANONICAL_ORDER)}
        envs_sorted = sorted(
            record.environments,
            key=lambda e: (env_rank.get(e.env_binding, len(env_rank)), e.env_binding),
        )
        files_sorted = sorted(record.files, key=lambda f: f.source_location)
        return {
            "target_id": record.target_id,
            "target_kind": record.target_kind,
            "environments": [e.to_dict() for e in envs_sorted],
            "files": [f.to_dict() for f in files_sorted],
        }

    def write(self, record: TargetRecord, path: Path) -> None:
        doc = self._canonical_record(record)
        self._loader._validate(doc)
        path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")

    def write_collection(
        self, coll: DeploymentCollection, path: str | Path
    ) -> None:
        records_sorted = sorted(
            (self._canonical_record(r) for r in coll),
            key=lambda d: str(d["target_id"]),
        )
        for doc in records_sorted:
            self._loader._validate(doc)
        # Wrap in an envelope so the file has a top-level `$schema` field
        # (Rule-008 requires every committed JSON to declare its schema).
        # On import to MongoDB the envelope dissolves: each entry in
        # `records[]` becomes one document in the collection.
        envelope = {
            "$schema": self._loader.schema_url,
            "records": records_sorted,
        }
        out = Path(path)
        out.write_text(
            json.dumps(envelope, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
