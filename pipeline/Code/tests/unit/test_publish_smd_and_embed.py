# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for v42 §5.2.8a: publish_smd_and_embed + specialty embed
composer + _nucc_lookup flip to read published SMD + orchestrator DAG.

Every test uses mongomock for the cluster surfaces and a fake OpenAI
client so no network egress happens. The stubs are the minimum surface
each production entry point actually touches; anything the production
code does not read is left off.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import mongomock
import pytest


# ---------- _compose_specialty_text ----------


@pytest.mark.unit
def test_compose_specialty_text_all_fields_present():
    from embedding_engine import _compose_specialty_text
    doc = {
        "Display Name": "Surgical Technologist",
        "Grouping": "Technologists, Technicians & Other Technical Service Providers",
        "Classification": "Specialist",
        "Specialization": "Surgical Assistant",
        "Definition": "Performs surgical support tasks under supervision.",
    }
    out = _compose_specialty_text(doc)
    assert out.startswith("Surgical Technologist | ")
    assert "Surgical Assistant" in out
    assert "surgical support" in out.lower()
    assert "|| " not in out  # no filler between empty fields


@pytest.mark.unit
def test_compose_specialty_text_skips_empty_fields():
    from embedding_engine import _compose_specialty_text
    out = _compose_specialty_text({
        "Display Name": "Foo",
        "Grouping": "",
        "Classification": None,
        "Specialization": "  ",
        "Definition": "Bar baz",
    })
    assert out == "Foo | Bar baz"


@pytest.mark.unit
def test_compose_specialty_text_all_empty_yields_empty_string():
    from embedding_engine import _compose_specialty_text
    assert _compose_specialty_text({"Display Name": "", "Definition": None}) == ""


# ---------- generate_specialty_embeddings ----------


class _FakeEmbedding:
    def __init__(self, vec: list[float]):
        self.embedding = vec


class _FakeEmbeddings:
    def __init__(self, dim: int):
        self._dim = dim
        self.calls: list[list[str]] = []

    def create(self, *, model: str, input: list[str]):
        assert model == "text-embedding-3-large"
        self.calls.append(list(input))

        class _Resp:
            pass
        r = _Resp()
        r.data = [_FakeEmbedding([0.001 * (i + 1)] * self._dim) for i in range(len(input))]
        return r


class _FakeOpenAIClient:
    def __init__(self, dim: int = 3072):
        self.embeddings = _FakeEmbeddings(dim)


def _mongomock_safe_bulk_write(coll, ops, ordered=False):
    """Adapter around mongomock 4.3.0's incompatibility with pymongo 4.x's
    UpdateOne — the modern UpdateOne carries a `sort` kwarg mongomock's
    BulkOperationBuilder doesn't accept. In production Atlas + real
    pymongo work fine; the shim only exists so tests can run offline.
    Iterates each op as an individual update_one call and returns a
    minimal result surface (modified_count) the caller consumes.
    """
    modified = 0
    for op in ops:
        # pymongo UpdateOne stores its filter/update on _filter/_doc.
        filt = getattr(op, "_filter", None)
        update = getattr(op, "_doc", None)
        if filt is None or update is None:
            continue
        res = coll.update_one(filt, update, upsert=False)
        modified += (res.modified_count or 0)

    class _R:
        pass
    r = _R()
    r.modified_count = modified
    r.inserted_count = 0
    return r


@pytest.fixture
def fake_openai(monkeypatch):
    client = _FakeOpenAIClient()

    def _fake_build(api_key):
        return client

    monkeypatch.setattr("embedding_engine._build_openai_client", _fake_build)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")

    # Route _process_batch's bulk_write through the mongomock-safe shim.
    import embedding_engine as _ee
    orig_process = _ee._process_batch

    def _patched_process(*, client, coll, batch):
        # Shim only the mongomock case; real pymongo collections pass
        # through untouched. mongomock collections have no `database` attr
        # of the pymongo shape; check for the mongomock module in the type.
        if "mongomock" in type(coll).__module__:
            orig_bulk = coll.bulk_write
            coll.bulk_write = lambda ops, ordered=False: _mongomock_safe_bulk_write(coll, ops, ordered)
            try:
                return orig_process(client=client, coll=coll, batch=batch)
            finally:
                coll.bulk_write = orig_bulk
        return orig_process(client=client, coll=coll, batch=batch)

    monkeypatch.setattr(_ee, "_process_batch", _patched_process)
    return client


@pytest.mark.unit
def test_generate_specialty_embeddings_writes_vector_and_metadata(fake_openai):
    from embedding_engine import generate_specialty_embeddings, CANONICAL_MODEL, CANONICAL_DIM
    client = mongomock.MongoClient()
    coll = client["PublicHealthData"]["SpecialtyMetaData_staging_v_3"]
    coll.insert_many([
        {"Code": "207W00000X", "Display Name": "Ophthalmology"},
        {"Code": "246ZS0400X", "Display Name": "Surgical Technologist", "is_supplemented": True},
    ])
    summary = generate_specialty_embeddings(
        {"specialty_collection": "PublicHealthData.SpecialtyMetaData_staging_v_3"},
        mongo=client,
    )
    assert summary["candidate_count"] == 2
    assert summary["updated_count"] == 2
    assert summary["failed_count"] == 0
    assert summary["model"] == CANONICAL_MODEL
    assert summary["dimensions"] == CANONICAL_DIM
    for row in coll.find({}):
        assert row["embedding_model"] == CANONICAL_MODEL
        assert isinstance(row["embedding"], list)
        assert len(row["embedding"]) == CANONICAL_DIM
        assert "embedding_generated_at" in row


@pytest.mark.unit
def test_generate_specialty_embeddings_skips_already_embedded(fake_openai):
    from embedding_engine import generate_specialty_embeddings, CANONICAL_MODEL, CANONICAL_DIM
    client = mongomock.MongoClient()
    coll = client["PublicHealthData"]["SpecialtyMetaData_staging_v_3"]
    coll.insert_one({
        "Code": "207W00000X",
        "Display Name": "Ophthalmology",
        "embedding": [0.0] * CANONICAL_DIM,
        "embedding_model": CANONICAL_MODEL,
    })
    coll.insert_one({"Code": "246ZS0400X", "Display Name": "Surgical Technologist"})
    summary = generate_specialty_embeddings(
        {"specialty_collection": "PublicHealthData.SpecialtyMetaData_staging_v_3"},
        mongo=client,
    )
    # Only the un-embedded row is a candidate; the pre-embedded one is skipped.
    assert summary["candidate_count"] == 1
    assert summary["updated_count"] == 1


@pytest.mark.unit
def test_generate_specialty_embeddings_skips_rows_with_no_composable_text(fake_openai):
    from embedding_engine import generate_specialty_embeddings
    client = mongomock.MongoClient()
    coll = client["PublicHealthData"]["SpecialtyMetaData_staging_v_3"]
    coll.insert_one({"Code": "ZZZ0000000X"})  # no Display Name / etc.
    summary = generate_specialty_embeddings(
        {"specialty_collection": "PublicHealthData.SpecialtyMetaData_staging_v_3"},
        mongo=client,
    )
    assert summary["candidate_count"] == 0
    assert summary["updated_count"] == 0


# ---------- publish_smd_and_embed step ----------


@dataclass
class _FakeArgs:
    data_version: int = 3
    env_prefix: str = "dev"


@dataclass
class _FakeManifest:
    run_id: str = "run-abc"


class _FakeCtx:
    """Minimal StepContext double publish_smd_and_embed.execute reads
    through PipelineRuntime(ctx). PipelineRuntime pulls: mongo_client,
    frontend (via get_frontend_mongo), env_prefix, run_id, data_version.
    """

    def __init__(self, mongo, frontend):
        self.mongo_client = mongo
        self.blob_client = None
        self.notification_client = None
        self.catalog_cache = None
        self.catalog = None
        self.step_summaries = {}
        self.config = {}
        self.args = _FakeArgs()
        self.manifest = _FakeManifest()
        self.env_prefix = "dev"
        self.run_id = "run-abc"
        # tests patch pipeline_db.get_frontend_mongo to return `frontend`,
        # so we retain a handle so tests can assert on frontend state.
        self._frontend = frontend


@pytest.fixture
def fake_clusters(monkeypatch):
    """Two independent mongomock clients so cross-cluster copy is real."""
    pipeline = mongomock.MongoClient()
    frontend = mongomock.MongoClient()
    monkeypatch.setattr("pipeline_runtime.get_frontend_mongo", lambda: frontend)
    monkeypatch.setattr("pipeline_runtime.get_mongo", lambda: pipeline)
    return pipeline, frontend


@pytest.mark.unit
def test_publish_smd_and_embed_end_to_end(fake_openai, fake_clusters, monkeypatch):
    from staging_loader import STAGING_DB_NAME, staging_collection_name
    from steps.publish_smd_and_embed import execute
    from embedding_engine import CANONICAL_MODEL, CANONICAL_DIM

    pipeline, frontend = fake_clusters
    # Seed StagingNucc on the pipeline cluster (v42: normalize_nucc's
    # output). Mix a native NUCC row + one F-105 supplement row.
    src = pipeline[STAGING_DB_NAME][staging_collection_name("nucc", 3)]
    src.insert_many([
        {
            "run_id": "run-abc",
            "Code": "207W00000X",
            "code": "207W00000X",
            "Display Name": "Ophthalmology",
            "Grouping": "Allopathic & Osteopathic Physicians",
            "Classification": "Ophthalmology",
            "Specialization": "",
            "Definition": "An ophthalmologist has the...",
            "is_supplemented": False,
            "can_prescribe": True,
            "is_homeopathic": False,
            "is_disqualified": False,
            "raw": {"Code": "207W00000X", "Display Name": "Ophthalmology"},
        },
        {
            "run_id": "run-abc",
            "Code": "246ZS0400X",
            "code": "246ZS0400X",
            "Display Name": "Surgical Technologist",
            "Grouping": "Technologists, Technicians & Other Technical Service Providers",
            "Classification": "Specialist",
            "Specialization": "Surgical Assistant",
            "Definition": "",
            "is_supplemented": True,
            "can_prescribe": False,
            "is_homeopathic": False,
            "is_disqualified": False,
            "raw": {"Code": "246ZS0400X", "Display Name": "Surgical Technologist"},
        },
        # A stale row from a prior run — MUST NOT be published.
        {"run_id": "run-old", "Code": "STALE00000X", "Display Name": "Stale"},
    ])

    # Pre-seed the LIVE SMD on the PIPELINE cluster (this is where SMD
    # now lives — pipelines never migrate to frontend) with a completely
    # different set of docs so we can prove the atomic swap DROPPED live
    # and REPLACED it with the staging contents — never a merge, never
    # a leftover.
    live = pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"]
    live.insert_many([
        {"Code": "OLD1111111X", "Display Name": "Old row 1"},
        {"Code": "OLD2222222X", "Display Name": "Old row 2"},
    ])

    ctx = _FakeCtx(pipeline, frontend)
    summary = execute(ctx)

    assert summary["rows_copied"] == 2  # stale run_id filtered out
    assert summary["embed_candidates"] == 2
    assert summary["embed_updated"] == 2
    assert summary["embed_failed"] == 0
    assert summary["smd_collection"] == "PublicHealthData.SpecialtyMetaData_v_3"

    # Post-swap: live collection on PIPELINE cluster holds the two
    # staged rows, each embedded.
    live_after = pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"]
    published = list(live_after.find({}))
    codes = sorted(r.get("Code") for r in published)
    assert codes == ["207W00000X", "246ZS0400X"]  # old rows gone, stale run gone
    for row in published:
        assert row["embedding_model"] == CANONICAL_MODEL
        assert len(row["embedding"]) == CANONICAL_DIM
    # Staging collection was renamed away — must no longer exist.
    assert "SpecialtyMetaData_staging_v_3" not in pipeline["PublicHealthData"].list_collection_names()
    # Pipeline-only fields stripped; run_id kept per operator rule 2026-08-02
    # so each SpecialtyMetaData row carries the run_id that loaded it, matching
    # the run_id on the _loaded_metadata doc for that collection.
    for row in published:
        assert "_source_row_index" not in row
        assert "raw" not in row
        assert row.get("run_id") == "run-abc"
    # FRONTEND cluster MUST remain untouched — pipelines never migrate.
    assert "SpecialtyMetaData_v_3" not in frontend["dev_PublicHealthData"].list_collection_names()
    assert "SpecialtyMetaData" not in frontend["dev_PublicHealthData"].list_collection_names()
    assert "SpecialtyMetaData_staging" not in frontend["dev_PublicHealthData"].list_collection_names()


@pytest.mark.unit
def test_publish_smd_and_embed_bails_when_openai_key_missing(fake_clusters, monkeypatch):
    from chathealthy_frontend_lib.exceptions import ChatHealthyException
    from steps.publish_smd_and_embed import execute
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pipeline, frontend = fake_clusters
    ctx = _FakeCtx(pipeline, frontend)
    with pytest.raises(ChatHealthyException) as exc_info:
        execute(ctx)
    assert "OPENAI_API_KEY" in str(exc_info.value)


@pytest.mark.unit
def test_publish_smd_and_embed_records_discrepancies_when_embed_fails(
    fake_openai, fake_clusters, monkeypatch,
):
    """Operator directive 2026-08-02: embed failure is a non-fatal
    error. Publish MUST still atomic-swap so SMD is usable for lookups,
    AND one discrepancy per un-embedded row MUST be written so the PDF
    report surfaces exactly which codes need re-embedding when OpenAI
    credits are topped."""
    from staging_loader import STAGING_DB_NAME, staging_collection_name
    from steps.publish_smd_and_embed import execute

    # Make every OpenAI embed call raise (simulates HTTP 429 insufficient_quota)
    import embedding_engine as _ee
    orig_embed_batch = _ee._embed_batch

    def _explode(client, texts):
        raise RuntimeError("insufficient_quota: You have no credits remaining")

    monkeypatch.setattr(_ee, "_embed_batch", _explode)

    pipeline, frontend = fake_clusters
    src = pipeline[STAGING_DB_NAME][staging_collection_name("nucc", 3)]
    src.insert_many([
        {"run_id": "run-abc", "Code": "207W00000X", "code": "207W00000X",
         "Display Name": "Ophthalmology", "is_supplemented": False,
         "raw": {"Code": "207W00000X"}},
        {"run_id": "run-abc", "Code": "246ZS0400X", "code": "246ZS0400X",
         "Display Name": "Surgical Technologist", "is_supplemented": True,
         "raw": {"Code": "246ZS0400X"}},
    ])
    ctx = _FakeCtx(pipeline, frontend)
    summary = execute(ctx)

    # Atomic swap MUST have fired on PIPELINE cluster: SMD_v_3 holds
    # the two rows.
    live = pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"]
    codes = sorted(r.get("Code") for r in live.find({}))
    assert codes == ["207W00000X", "246ZS0400X"]

    # Neither row has an embedding field (OpenAI failed).
    for row in live.find({}):
        assert "embedding" not in row
        assert "embedding_model" not in row

    # Summary carries the count.
    assert summary["embed_updated"] == 0
    assert summary["embed_failed"] == 2
    assert summary["embed_unembedded_rows"] == 2

    # FRONTEND cluster MUST remain untouched.
    assert "SpecialtyMetaData" not in frontend["dev_PublicHealthData"].list_collection_names()
    assert "SpecialtyMetaData_v_3" not in frontend["dev_PublicHealthData"].list_collection_names()

    # ONE discrepancy per un-embedded row, reason prefixed 'error_'.
    disc = frontend["chathealthyfrontend"]["pipeline.discrepancies"]
    rows = list(disc.find({"reason": "error_specialty_embedding_failed"}))
    assert len(rows) == 2
    disc_codes = sorted(r["detail"]["code"] for r in rows)
    assert disc_codes == ["207W00000X", "246ZS0400X"]
    for r in rows:
        assert r["step"] == "publish_smd_and_embed"
        assert r["entity_kind"] == "specialty"
        assert r["npi"] is None
        assert "note" in r["detail"]

    # Restore for later tests in the same session
    monkeypatch.setattr(_ee, "_embed_batch", orig_embed_batch)


# ---------- _nucc_lookup (post-v42 SMD reader) ----------


@pytest.mark.unit
def test_nucc_lookup_reads_from_published_smd(fake_clusters):
    from pipeline_runtime import PipelineRuntime
    from provider_normalize_engine import _nucc_lookup

    pipeline, frontend = fake_clusters
    # SMD lives on the PIPELINE cluster's PublicHealthData.SpecialtyMetaData_v_N
    # per operator directive 2026-08-02 (pipelines never touch frontend).
    smd = pipeline["PublicHealthData"]["SpecialtyMetaData_v_3"]
    smd.insert_many([
        {"Code": "207W00000X", "Display Name": "Ophthalmology"},
        {"Code": "246ZS0400X", "Display Name": "Surgical Technologist"},
    ])

    ctx = _FakeCtx(pipeline, frontend)
    rt = PipelineRuntime(ctx)
    out = _nucc_lookup(rt)
    assert set(out.keys()) == {"207W00000X", "246ZS0400X"}
    # Shape wraps the SMD row in {'raw': ...} so build_provider_record's
    # existing raw.get('Display Name') read still works unchanged.
    assert out["246ZS0400X"]["raw"]["Display Name"] == "Surgical Technologist"


# ---------- orchestrator DAG ----------


@pytest.mark.unit
def test_orchestrator_publish_smd_between_normalize_nucc_and_normalize_fanout():
    from provider_pipeline_orchestrator import ProviderPipelineOrchestrator
    steps = list(ProviderPipelineOrchestrator.STEPS)
    names = [s.name for s in steps]
    by_name = {s.name: s for s in steps}
    assert "publish_smd_and_embed" in by_name, "new v42 step missing from STEPS"
    pub = by_name["publish_smd_and_embed"]
    fanout = by_name["normalize_npi_per_state_fanout"]
    assert "normalize_nucc" in pub.prerequisites
    assert "publish_smd_and_embed" in fanout.prerequisites, (
        "code_label ordering fix: fanout must gate on published SMD"
    )
    # Positional-order invariant: BasePipelineOrchestrator walks STEPS in
    # array order and enforces prereqs at first visit, so every step MUST
    # appear AFTER every one of its prereqs in this list. Fire 2026-08-02
    # crashed because normalize_npi_per_state_fanout preceded its own
    # prereq publish_smd_and_embed in the array — prereq check fired
    # before the scheduler ever dispatched the earlier-topologically step.
    for spec in steps:
        idx = names.index(spec.name)
        for pre in spec.prerequisites:
            assert pre in names, f"{spec.name!r} names unknown prereq {pre!r}"
            assert names.index(pre) < idx, (
                f"topological order broken: {spec.name!r} at position "
                f"{idx} lists prereq {pre!r} at position {names.index(pre)}"
            )
