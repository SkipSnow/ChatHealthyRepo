# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""pipeline_message — self-describing record envelope for the streaming pipeline.

Replaces store-and-forward through Mongo between every stage. Each NPI is one
PipelineMessage that flows through stage activities, accumulating its
content_schema_conformant payload as stages mutate it.

The schema (Website/schemas/ChatHealthyProvidersSchema.json) is the CONTRACT.
At end-of-flow (mongo_write_stage_fn) the message's `content` is the
provider document that gets upserted. Intermediate stages are free to extend
the envelope; the schema check happens at the boundary.

Fields
------
npi : str
    Business key. Required.
raw : dict | None
    The original NPPES CSV row as parsed by Raw-Parse stage. Cleared by
    Normalize stage (set to None) once `content` carries the equivalent.
content : dict
    The provider document being assembled. After Normalize, conforms to
    ChatHealthyProvidersSchema.json shape (addresses[], active[], etc.).
    Stages mutate this in-place.
meta : dict
    Pipeline metadata carried with the message so receivers don't have to
    track state externally. Fields:
        load_id    — parent orchestration instance_id
        stage      — current stage name (set by each stage on entry)
        history    — list of {stage, started_at, finished_at} entries
        record_id  — monotonic per-partition counter (raw-parse assigns)
        worker_id  — partition number (raw-parse assigns)
        partition  — (start_byte, end_byte) tuple
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


@dataclass
class PipelineMessage:
    npi: str
    raw: dict | None = None
    content: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    @classmethod
    def from_raw_row(
        cls,
        npi: str,
        raw_row: dict,
        load_id: str,
        record_id: int,
        worker_id: int,
        partition: tuple,
    ) -> "PipelineMessage":
        return cls(
            npi=npi,
            raw=raw_row,
            content={"npi": npi},
            meta={
                "load_id":   load_id,
                "stage":     "raw_parse",
                "history":   [{"stage": "raw_parse",
                               "finished_at": datetime.now(timezone.utc).isoformat()}],
                "record_id": record_id,
                "worker_id": worker_id,
                "partition": partition,
            },
        )

    def enter_stage(self, stage_name: str) -> None:
        """Record stage entry. Caller passes its stage name on receive."""
        self.meta["stage"] = stage_name
        # The previous stage's history entry already carries finished_at;
        # we don't track per-stage start time at the message level because
        # batch processing makes per-record start ambiguous.

    def exit_stage(self, stage_name: str) -> None:
        """Record stage exit + append history entry."""
        self.meta["history"].append({
            "stage":       stage_name,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })

    def to_wire(self) -> dict:
        """Serialize to a wire-safe dict for transport between activities."""
        return asdict(self)

    @classmethod
    def from_wire(cls, d: dict) -> "PipelineMessage":
        return cls(
            npi=d["npi"],
            raw=d.get("raw"),
            content=d.get("content") or {"npi": d["npi"]},
            meta=d.get("meta") or {},
        )

    def to_mongo_doc(self) -> dict:
        """Final Mongo document. Strips internal meta — only schema-conformant
        fields land in the collection. `loaded_at` is taken from the latest
        history entry (raw_parse marks the load time per record)."""
        doc = dict(self.content)
        doc["load_id"] = self.meta.get("load_id")
        doc["record_id"] = self.meta.get("record_id")
        # loaded_at = first history entry (raw_parse stamp)
        hist = self.meta.get("history") or []
        if hist:
            doc["loaded_at"] = hist[0].get("finished_at")
        return doc
