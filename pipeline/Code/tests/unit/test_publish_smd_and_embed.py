# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for v42 §5.2.8a: publish_smd_and_embed + specialty embed
composer + _nucc_lookup flip to read published SMD + orchestrator DAG.

Cluster surfaces are real: the tests reach MongoDB through the canonical
utility as DevOpsUser and work in scratch collections carrying a per-run
uuid, dropped afterwards. OpenAI is the one thing stood in for, because
embedding text is a paid external call and none of these assertions are
about what the vector contains.

The collection and database names the code resolves are redirected to
those scratch names, which is what keeps a test run out of production
data while leaving every MongoDB behaviour under test genuine.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pytest
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402


def _scratch_config(db_name: str, prefix: str) -> dict:
    """A dataset_versions[] config resolving to scratch collections.

    Staging and loaded share a database because renameCollection is a
    same-database operation — the constraint the production layout is
    built around.
    """
    def entry(source_name: str, staging_base: str, public_base: str) -> dict:
        return {
            "source_name": source_name,
            "fetch": {"source_url": f"https://example.com/{source_name}.csv"},
            "file_format": "csv",
            "staging_name": f"{db_name}.{prefix}{staging_base}",
            "public_data_name": f"{db_name}.{prefix}{public_base}",
        }

    # Same base names production uses, so the step resolves the same
    # entries; only the database and the uuid prefix differ.
    return {
        "dataset_versions": [
            entry("nucc", "StagingNucc", "Nucc"),
            entry("smd", "SpecialtyMetaData_staging", "SpecialtyMetaData"),
        ],
    }


def _test_registry(pipeline_mongo, cfg):
    from pipeline_dataset_registry import PipelineDatasetRegistry
    return PipelineDatasetRegistry(cfg, 3, pipeline_mongo)


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


@pytest.fixture
def fake_openai(monkeypatch):
    """Stand in for the embedding provider only.

    The vector's contents are not what any of these tests assert, and a
    real call is billed per token. Everything downstream of it — the
    bulk write, the update, the collection state — runs against the real
    server.
    """
    client = _FakeOpenAIClient()

    def _fake_build(api_key):
        return client

    monkeypatch.setattr("embedding_engine._build_openai_client", _fake_build)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-key")
    return client


@pytest.fixture
def specialty_staging(scratch_mongo):
    """A real, disposable staging collection plus its dotted name."""
    db, collection = scratch_mongo
    coll = collection("SpecialtyMetaData_staging_v_3")
    return coll, f"{db.name}.{coll.name}"


@pytest.mark.unit
def test_generate_specialty_embeddings_writes_vector_and_metadata(
    fake_openai, specialty_staging,
):
    from embedding_engine import generate_specialty_embeddings, CANONICAL_MODEL, CANONICAL_DIM
    coll, dotted = specialty_staging
    coll.insert_many([
        {"Code": "207W00000X", "Display Name": "Ophthalmology"},
        {"Code": "246ZS0400X", "Display Name": "Surgical Technologist", "is_supplemented": True},
    ])
    summary = generate_specialty_embeddings(
        {"specialty_collection": dotted},
        mongo=coll.database.client,
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
def test_generate_specialty_embeddings_skips_already_embedded(
    fake_openai, specialty_staging,
):
    from embedding_engine import generate_specialty_embeddings, CANONICAL_MODEL, CANONICAL_DIM
    coll, dotted = specialty_staging
    coll.insert_one({
        "Code": "207W00000X",
        "Display Name": "Ophthalmology",
        "embedding": [0.0] * CANONICAL_DIM,
        "embedding_model": CANONICAL_MODEL,
    })
    coll.insert_one({"Code": "246ZS0400X", "Display Name": "Surgical Technologist"})
    summary = generate_specialty_embeddings(
        {"specialty_collection": dotted},
        mongo=coll.database.client,
    )
    # Only the un-embedded row is a candidate; the pre-embedded one is skipped.
    assert summary["candidate_count"] == 1
    assert summary["updated_count"] == 1


@pytest.mark.unit
def test_generate_specialty_embeddings_skips_rows_with_no_composable_text(
    fake_openai, specialty_staging,
):
    from embedding_engine import generate_specialty_embeddings
    coll, dotted = specialty_staging
    coll.insert_one({"Code": "ZZZ0000000X"})  # no Display Name / etc.
    summary = generate_specialty_embeddings(
        {"specialty_collection": dotted},
        mongo=coll.database.client,
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
    env_prefix, run_id, data_version. Post-2026-08-03 there is no
    separate frontend mongo -- coord lives on the pipeline cluster.
    """

    def __init__(self, mongo, frontend=None):
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
        # `frontend` retained for backward-compat with tests still passing
        # a second arg; NOT used at runtime.
        self._frontend = frontend


@pytest.fixture
def clusters(monkeypatch, scratch_mongo):
    """A real cluster with every destination redirected to scratch names.

    renameCollection is performed by the server, so what the swap does to
    the collections is observed rather than simulated. The previous
    simulation of it was written against a fake that does not implement
    the command at all.

    The frontend handle is a connection to the actual frontend cluster,
    not a second handle on the pipeline one, so the assertion that
    pipelines never migrate to frontend is checked against the cluster
    it is a claim about.

    Yields (client, scratch_db, frontend_db, cfg, names).
    """
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

    import pipeline_loaded_metadata
    import pipeline_runtime

    db, collection = scratch_mongo
    client = db.client
    prefix = collection("").name  # the run's unique prefix
    cfg = _scratch_config(db.name, prefix)

    frontend = ChatHealthyMongoUtilities().getConnection("DevOpsUser", "ChatHealthyFrontEnd")
    frontend_db = frontend[db.name]

    discrepancies = frontend_db[f"{prefix}discrepancies"]
    monkeypatch.setattr(
        pipeline_runtime.PipelineRuntime, "discrepancies_coll",
        property(lambda self: discrepancies),
    )
    monkeypatch.setattr(pipeline_runtime, "get_frontend_mongo", lambda: frontend)
    monkeypatch.setattr(pipeline_runtime, "get_mongo", lambda *_: client)
    monkeypatch.setattr(pipeline_runtime, "load_pipeline_config", lambda **kw: cfg)
    monkeypatch.setattr(pipeline_loaded_metadata, "_METADATA_DB", db.name)
    monkeypatch.setattr(pipeline_loaded_metadata, "_METADATA_COLL",
                        collection("loaded_metadata").name)

    names = {
        "db": db.name,
        "prefix": prefix,
        "live": f"{prefix}SpecialtyMetaData_v_3",
        "staging": f"{prefix}SpecialtyMetaData_staging_v_3",
        "discrepancies": discrepancies.name,
        "live_dotted": f"{db.name}.{prefix}SpecialtyMetaData_v_3",
    }
    try:
        yield client, db, frontend_db, cfg, names
    finally:
        # The frontend cluster should hold nothing from this run; drop
        # anything that appeared so a failure does not leave residue.
        for name in frontend_db.list_collection_names():
            if name.startswith(prefix):
                frontend_db.drop_collection(name)


@pytest.mark.unit
def test_publish_smd_and_embed_end_to_end(fake_openai, clusters, monkeypatch):
    from staging_loader import staging_collection_name, staging_db_name
    from steps.publish_smd_and_embed import execute
    from embedding_engine import CANONICAL_MODEL, CANONICAL_DIM

    pipeline, db, frontend_db, cfg, names = clusters
    # Seed the NUCC staging collection on the pipeline cluster (v42:
    # normalize_nucc's output). Names come through the registry so the
    # test stays coupled to dataset_versions[] rather than hardcoding
    # the collection names in two places. Mix a native NUCC row + one
    # F-105 supplement row.
    reg = _test_registry(pipeline, cfg)
    src = pipeline[staging_db_name(reg, "nucc")][staging_collection_name(reg, "nucc")]
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
    live = db[names["live"]]
    live.insert_many([
        {"Code": "OLD1111111X", "Display Name": "Old row 1"},
        {"Code": "OLD2222222X", "Display Name": "Old row 2"},
    ])

    ctx = _FakeCtx(pipeline, frontend_db.client)
    summary = execute(ctx)

    assert summary["rows_copied"] == 2  # stale run_id filtered out
    assert summary["embed_candidates"] == 2
    assert summary["embed_updated"] == 2
    assert summary["embed_failed"] == 0
    assert summary["smd_collection"] == f"{names['db']}.{names['live']}"

    # Post-swap: live collection holds the two staged rows, each embedded.
    published = list(db[names["live"]].find({}))
    codes = sorted(r.get("Code") for r in published)
    assert codes == ["207W00000X", "246ZS0400X"]  # old rows gone, stale run gone
    for row in published:
        assert row["embedding_model"] == CANONICAL_MODEL
        assert len(row["embedding"]) == CANONICAL_DIM
    # Staging collection was renamed away — must no longer exist.
    assert names["staging"] not in db.list_collection_names()
    # Pipeline-only fields stripped; run_id kept per operator rule 2026-08-02
    # so each SpecialtyMetaData row carries the run_id that loaded it, matching
    # the run_id on the _loaded_metadata doc for that collection.
    for row in published:
        assert "_source_row_index" not in row
        assert "raw" not in row
        assert row.get("run_id") == "run-abc"
    # The FRONTEND cluster MUST remain untouched — pipelines never
    # migrate. Checked against the frontend cluster itself.
    on_frontend = frontend_db.list_collection_names()
    assert names["live"] not in on_frontend
    assert names["staging"] not in on_frontend


@pytest.mark.unit
def test_publish_smd_and_embed_bails_when_openai_key_missing(clusters, monkeypatch):
    from chathealthy_lib.exceptions import ChatHealthyException
    from steps.publish_smd_and_embed import execute
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    pipeline, _db, frontend_db, _cfg, _names = clusters
    ctx = _FakeCtx(pipeline, frontend_db.client)
    with pytest.raises(ChatHealthyException) as exc_info:
        execute(ctx)
    assert "OPENAI_API_KEY" in str(exc_info.value)


@pytest.mark.unit
def test_publish_smd_and_embed_records_discrepancies_when_embed_fails(
    fake_openai, clusters, monkeypatch,
):
    """Operator directive 2026-08-02: embed failure is a non-fatal
    error. Publish MUST still atomic-swap so SMD is usable for lookups,
    AND one discrepancy per un-embedded row MUST be written so the PDF
    report surfaces exactly which codes need re-embedding when OpenAI
    credits are topped."""
    from staging_loader import staging_collection_name, staging_db_name
    from steps.publish_smd_and_embed import execute

    # Make every OpenAI embed call fail the way the provider fails when
    # the account is out of credit: an HTTP 429 from the OpenAI client,
    # not a generic built-in.
    import httpx
    import openai

    import embedding_engine as _ee
    orig_embed_batch = _ee._embed_batch

    def _explode(client, texts):
        request = httpx.Request("POST", "https://api.openai.com/v1/embeddings")
        response = httpx.Response(429, request=request)
        raise ChatHealthyException(
            mode="rate_limited",
            component="test_publish_smd_and_embed",
            message="insufficient_quota: You have no credits remaining",
            response=response,
            body=None,)

    monkeypatch.setattr(_ee, "_embed_batch", _explode)

    pipeline, db, frontend_db, cfg, names = clusters
    reg = _test_registry(pipeline, cfg)
    src = pipeline[staging_db_name(reg, "nucc")][staging_collection_name(reg, "nucc")]
    src.insert_many([
        {"run_id": "run-abc", "Code": "207W00000X", "code": "207W00000X",
         "Display Name": "Ophthalmology", "is_supplemented": False,
         "raw": {"Code": "207W00000X"}},
        {"run_id": "run-abc", "Code": "246ZS0400X", "code": "246ZS0400X",
         "Display Name": "Surgical Technologist", "is_supplemented": True,
         "raw": {"Code": "246ZS0400X"}},
    ])
    ctx = _FakeCtx(pipeline, frontend_db.client)
    summary = execute(ctx)

    # Atomic swap MUST have fired on PIPELINE cluster: SMD_v_3 holds
    # the two rows.
    live = db[names["live"]]
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

    # FRONTEND cluster MUST remain untouched by the SMD collections.
    on_frontend = frontend_db.list_collection_names()
    assert names["live"] not in on_frontend
    assert names["staging"] not in on_frontend

    # ONE discrepancy per un-embedded row, reason prefixed 'error_'.
    disc = frontend_db[names["discrepancies"]]
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
def test_nucc_lookup_reads_from_published_smd(clusters):
    from pipeline_runtime import PipelineRuntime
    from provider_normalize_engine import _nucc_lookup

    pipeline, db, frontend_db, _cfg, names = clusters
    # SMD lives on the PIPELINE cluster per operator directive 2026-08-02
    # (pipelines never touch frontend); the registry resolves the name.
    smd = db[names["live"]]
    smd.insert_many([
        {"Code": "207W00000X", "Display Name": "Ophthalmology"},
        {"Code": "246ZS0400X", "Display Name": "Surgical Technologist"},
    ])

    ctx = _FakeCtx(pipeline, frontend_db.client)
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
