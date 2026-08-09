# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Unit tests for generic_pipeline_executor.GenericPipelineExecutor."""

from __future__ import annotations

import hashlib
import io
import zipfile

import pytest

from chathealthy_frontend_lib.exceptions import ChatHealthyException

from generic_pipeline_executor import GenericPipelineExecutor
from pipeline_dataset_registry import PipelineDatasetRegistry


# ---------------------------------------------------------------------------
# fake mongo capable of representing loaded_metadata + staging + public data
# ---------------------------------------------------------------------------


class _FakeCursor:
    def __init__(self, docs):
        self._docs = list(docs)

    def __iter__(self):
        return iter(self._docs)


class _FakeColl:
    def __init__(self, name: str):
        self.name = name
        self.docs: dict = {}
        self.inserts: list[dict] = []

    def find_one(self, filt):
        _id = filt.get("_id")
        return self.docs.get(_id)

    def update_one(self, filt, update, upsert=False):
        _id = filt.get("_id")
        setter = update.get("$set", {})
        current = dict(self.docs.get(_id) or {})
        current.update(setter)
        current["_id"] = _id
        self.docs[_id] = current

    def insert_one(self, doc):
        self.inserts.append(dict(doc))

    def count_documents(self, filt):
        # Only used on staging/public colls; store rows separately below.
        return len(self.docs)

    def clear(self):
        self.docs.clear()


class _FakeDb:
    def __init__(self, name: str):
        self.name = name
        self.colls: dict[str, _FakeColl] = {}
        self._present: set[str] = set()

    def __getitem__(self, coll_name):
        c = self.colls.get(coll_name)
        if c is None:
            c = _FakeColl(coll_name)
            self.colls[coll_name] = c
        self._present.add(coll_name)
        return c

    def list_collection_names(self):
        return sorted(self._present)


class _FakeAdmin:
    def __init__(self, mongo):
        self.mongo = mongo
        self.commands: list[dict] = []

    def command(self, cmd):
        self.commands.append(dict(cmd))
        if not isinstance(cmd, dict):
            return {"ok": 1}
        if cmd.get("renameCollection"):
            src = cmd["renameCollection"]
            dst = cmd["to"]
            src_db, src_coll = src.split(".", 1)
            dst_db, dst_coll = dst.split(".", 1)
            src_coll_obj = self.mongo.dbs.setdefault(src_db, _FakeDb(src_db))[src_coll]
            dst_db_obj = self.mongo.dbs.setdefault(dst_db, _FakeDb(dst_db))
            dst_db_obj.colls[dst_coll] = src_coll_obj
            dst_db_obj._present.add(dst_coll)
            src_db_obj = self.mongo.dbs.setdefault(src_db, _FakeDb(src_db))
            src_db_obj.colls.pop(src_coll, None)
            src_db_obj._present.discard(src_coll)
        return {"ok": 1}


class _FakeMongo:
    def __init__(self):
        self.dbs: dict[str, _FakeDb] = {}
        self.admin = _FakeAdmin(self)

    def __getitem__(self, db_name):
        db = self.dbs.get(db_name)
        if db is None:
            db = _FakeDb(db_name)
            self.dbs[db_name] = db
        return db


class _FakeBlobClient:
    """blob_client with http_get + put_blob attributes."""

    def __init__(self, http_get_response: bytes):
        self._body = http_get_response
        self.gets: list[str] = []
        self.uploads: list[tuple[str, str, int]] = []

    def http_get(self, url: str) -> bytes:
        self.gets.append(url)
        return self._body

    def put_blob(self, container: str, path: str, payload: bytes) -> None:
        self.uploads.append((container, path, len(payload)))


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _archive_bytes(members: dict[str, bytes]) -> bytes:
    """Build a real zip byte payload with the given filename -> bytes entries."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_STORED) as zf:
        for name, payload in members.items():
            zf.writestr(name, payload)
    return buf.getvalue()


def _config_with_bundle() -> dict:
    return {
        "dataset_versions": [
            {
                "source_name": "nppes_npi",
                "bundled_with": "nppes_zip",
                "fetch": {"source_url": "https://example.com/nppes.zip"},
                "archive_member": "npidata*.csv",
                "file_format": "csv",
                "staging_name": "PublicStaging.StagingNppes",
                "public_data_name": "PublicHealthData.NppesNpi",
            },
            {
                "source_name": "pl_pfile",
                "bundled_with": "nppes_zip",
                "archive_member": "pl_pfile*.csv",
                "file_format": "csv",
                "staging_name": "PublicStaging.StagingPl",
                "public_data_name": "PublicHealthData.PlPfile",
            },
            {
                "source_name": "smd",
                "staging_name": "PublicStaging.StagingSmd",
                "public_data_name": "PublicHealthData.Smd",
                "depends_on": ["nppes_npi"],
            },
            {
                "source_name": "provider",
                "staging_name": "PublicStaging.StagingProvider",
                "public_data_name": "PublicHealthData.Provider",
                "depends_on": ["nppes_npi", "pl_pfile", "smd"],
            },
        ],
    }


@pytest.fixture
def fake_mongo():
    return _FakeMongo()


@pytest.fixture
def registry(fake_mongo):
    return PipelineDatasetRegistry(_config_with_bundle(), 3, fake_mongo)


# ---------------------------------------------------------------------------
# fetch_phase - all unchanged (prior hash matches -> all fresh, no upload)
# ---------------------------------------------------------------------------


def test_fetch_phase_all_unchanged(fake_mongo, registry):
    archive = _archive_bytes({
        "npidata_pfile_a.csv": b"a,b,c\n",
        "pl_pfile_a.csv": b"1,2,3\n",
    })
    known_hash = hashlib.sha256(archive).hexdigest()
    # Seed metadata for the fetcher (nppes_npi) with the same hash.
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    meta.update_one(
        {"_id": "nppes_npi"},
        {"$set": {"source_name": "nppes_npi", "bundle_hash": known_hash,
                  "freshness": "fresh", "kind": "bundled"}},
        upsert=True,
    )
    blob = _FakeBlobClient(archive)
    executor = GenericPipelineExecutor(registry, fake_mongo, blob, "run-abc")
    summary = executor.fetch_phase()
    assert summary["nppes_npi"] == "fresh"
    assert summary["pl_pfile"] == "fresh"
    # No re-uploads on the unchanged path.
    assert blob.uploads == []


def test_fetch_phase_bundled_hash_changed_marks_owner_and_siblings_stale(
    fake_mongo, registry,
):
    archive = _archive_bytes({
        "npidata_pfile_a.csv": b"new,data\n",
        "pl_pfile_a.csv": b"new,rows\n",
    })
    # Seed a DIFFERENT bundle_hash so the executor sees a change.
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    meta.update_one(
        {"_id": "nppes_npi"},
        {"$set": {"bundle_hash": "stale_hash_from_last_run",
                  "freshness": "fresh", "kind": "bundled"}},
        upsert=True,
    )
    blob = _FakeBlobClient(archive)
    executor = GenericPipelineExecutor(registry, fake_mongo, blob, "run-xyz")
    summary = executor.fetch_phase()
    assert summary["nppes_npi"] == "stale"
    assert summary["pl_pfile"] == "stale"
    # Both bundle members' extracted files uploaded.
    upload_paths = [u[1] for u in blob.uploads]
    assert any("nppes_npi" in p for p in upload_paths)
    assert any("pl_pfile" in p for p in upload_paths)


# ---------------------------------------------------------------------------
# freshness_cascade_phase - stale upstream propagates to derived
# ---------------------------------------------------------------------------


def test_freshness_cascade_stale_upstream_makes_derived_stale(fake_mongo, registry):
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    # smd depends on nppes_npi; nppes_npi is stale.
    meta.update_one({"_id": "nppes_npi"},
                    {"$set": {"freshness": "stale", "kind": "bundled"}}, upsert=True)
    meta.update_one({"_id": "pl_pfile"},
                    {"$set": {"freshness": "fresh", "kind": "bundled"}}, upsert=True)
    executor = GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "run-c")
    summary = executor.freshness_cascade_phase()
    # smd depends on nppes_npi (stale) -> smd stale
    assert summary["smd"] == "stale"
    # provider depends on nppes_npi (stale) -> provider stale
    assert summary["provider"] == "stale"


def test_freshness_cascade_all_fresh_upstream_makes_derived_fresh(fake_mongo, registry):
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    for name in ("nppes_npi", "pl_pfile"):
        meta.update_one({"_id": name},
                        {"$set": {"freshness": "fresh", "kind": "bundled"}}, upsert=True)
    executor = GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "run-d")
    summary = executor.freshness_cascade_phase()
    assert summary["smd"] == "fresh"
    assert summary["provider"] == "fresh"


def test_freshness_cascade_visits_topological_order(fake_mongo, registry):
    """The cascade evaluates entries in registry.topological_order so a
    derived entry sees its dependencies' verdicts before its own decision."""
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    meta.update_one({"_id": "nppes_npi"},
                    {"$set": {"freshness": "fresh", "kind": "bundled"}}, upsert=True)
    meta.update_one({"_id": "pl_pfile"},
                    {"$set": {"freshness": "fresh", "kind": "bundled"}}, upsert=True)
    executor = GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "run-e")
    summary = executor.freshness_cascade_phase()
    order = [e.source_name for e in registry.topological_order()]
    # smd must come before provider (provider depends on smd).
    assert order.index("smd") < order.index("provider")
    assert summary["smd"] == "fresh"
    assert summary["provider"] == "fresh"


# ---------------------------------------------------------------------------
# stage_and_publish_phase
# ---------------------------------------------------------------------------


def test_stage_and_publish_skips_fresh_entries(fake_mongo, registry):
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    for name in ("nppes_npi", "pl_pfile", "smd", "provider"):
        meta.update_one({"_id": name},
                        {"$set": {"freshness": "fresh", "kind": "fetched"}}, upsert=True)
    executor = GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "run-f")
    report = executor.stage_and_publish_phase()
    for name in ("nppes_npi", "pl_pfile", "smd", "provider"):
        assert report[name]["published"] is False
        assert report[name]["reason"] == "already fresh"
    # No rename commands issued.
    assert fake_mongo.admin.commands == []


def test_stage_and_publish_renames_stale_entries(fake_mongo, registry):
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    meta.update_one({"_id": "smd"},
                    {"$set": {"freshness": "stale", "kind": "derived"}}, upsert=True)
    # Seed the staging collection so the rename target exists.
    fake_mongo["PublicStaging"]["StagingSmd_v_3"].insert_one({"code": "1"})
    executor = GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "run-g")
    report = executor.stage_and_publish_phase()
    assert report["smd"]["published"] is True
    # renameCollection command issued
    rename_cmds = [c for c in fake_mongo.admin.commands if "renameCollection" in c]
    assert len(rename_cmds) == 1
    assert rename_cmds[0]["renameCollection"] == "PublicStaging.StagingSmd_v_3"
    assert rename_cmds[0]["to"] == "PublicHealthData.Smd_v_3"
    assert rename_cmds[0]["dropTarget"] is True
    # Post-publish metadata marked fresh.
    fresh_meta = meta.find_one({"_id": "smd"})
    assert fresh_meta["freshness"] == "fresh"


def test_stage_and_publish_missing_staging_reports_unpublished(fake_mongo, registry):
    meta = fake_mongo["pipelineAdmin"]["pipeline.loaded_metadata"]
    meta.update_one({"_id": "provider"},
                    {"$set": {"freshness": "stale", "kind": "derived"}}, upsert=True)
    # Do NOT seed the staging collection.
    executor = GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "run-h")
    report = executor.stage_and_publish_phase()
    assert report["provider"]["published"] is False
    assert "staging collection" in report["provider"]["reason"]


# ---------------------------------------------------------------------------
# constructor guards - fatals recorded
# ---------------------------------------------------------------------------


def test_ctor_rejects_wrong_registry_type(fake_mongo):
    with pytest.raises(ChatHealthyException) as ei:
        GenericPipelineExecutor(object(), fake_mongo, _FakeBlobClient(b""), "run-i")
    assert ei.value.mode == "pipeline_executor_registry_wrong_type"
    # Fatal was recorded on the pipeline cluster.
    discs = fake_mongo["pipelineAdmin"]["pipeline.discrepancies"].inserts
    assert any(d["reason"] == "fatal_pipeline_executor_registry_wrong_type" for d in discs)


def test_ctor_rejects_missing_run_id(fake_mongo, registry):
    with pytest.raises(ChatHealthyException) as ei:
        GenericPipelineExecutor(registry, fake_mongo, _FakeBlobClient(b""), "")
    assert ei.value.mode == "pipeline_executor_run_id_missing"
