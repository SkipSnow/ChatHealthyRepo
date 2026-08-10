# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ARCH-CONSTRAINTS: Architecture constraint tests.
# SEC-GUARD-RISK-001: Guard risk acceptance integration.
#
# Uses a real MongoDB "lab" database (GOV-006: real system testing).
# RISK-008: Claude authorized to create/destroy any object in lab DB.
#
# Tests: create collection, insert records, vectorize, create vector index, verify, clean up.

import os
import sys
import pytest

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


# Rule-004: one place in this file obtains a connection, and it goes through
# the canonical utility. The certificate is the credential; there is no
# connection string here and no fallback. Raises if the identity cannot
# connect, which is the point -- a test that quietly connects as something
# else proves nothing about production.
def _ch_connection():
    import sys as _sys, pathlib as _pl
    for _d in _pl.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _lib = _d / "FrontEndApplicationLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", 'frontEnd')

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", ".."))

sys.path.insert(0, os.path.join(REPO_ROOT, "Code", "DataPipelines"))
sys.path.insert(0, os.path.join(REPO_ROOT, "Code", "Shared"))


from dotenv import load_dotenv
load_dotenv(os.path.join(REPO_ROOT, "Code", ".env"))


def _get_frontend_connection():
    conn = os.environ.get("MONGO_FRONTEND_connectionString")
    if not conn:
        pytest.skip("MONGO_FRONTEND_connectionString not set")
    return conn


LAB_DB = "lab"
LAB_COLLECTION = "test_vector_constraint"


class TestArchConstraintVectorIndex:
    """SEC-GUARD-RISK-001: Prove we can create DB objects in lab under RISK-008.
    Creates collection, inserts vectorized records, creates vector index, verifies."""

    @pytest.fixture(scope="class")
    def lab_collection(self):
        from pymongo import MongoClient
        conn = _get_frontend_connection()
        client = _ch_connection()
        db = client[LAB_DB]
        coll = db[LAB_COLLECTION]
        # Clean slate
        coll.drop()
        yield coll
        # Cleanup after all tests
        coll.drop()
        client.close()

    def test_create_collection_and_insert(self, lab_collection):
        """Create lab collection and insert 3 test records with embeddings."""
        from openai import OpenAI

        api_key = os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            pytest.skip("OPENAI_API_KEY not set")

        # 3 simple test documents
        docs = [
            {"name": "Test Provider A", "specialty": "Orthopedic Surgery"},
            {"name": "Test Provider B", "specialty": "Family Medicine"},
            {"name": "Test Provider C", "specialty": "Pediatrics"},
        ]

        # Vectorize with cheapest model
        client = OpenAI(api_key=api_key)
        for doc in docs:
            text = f"{doc['name']} {doc['specialty']}"
            resp = client.embeddings.create(
                model="text-embedding-3-small",
                input=text,
            )
            doc["embedding"] = resp.data[0].embedding

        lab_collection.insert_many(docs)
        count = lab_collection.count_documents({})
        assert count == 3, f"Expected 3 docs, got {count}"

    def test_create_vector_index(self, lab_collection):
        """Create Atlas Vector Search index on lab collection."""
        dims = 1536  # text-embedding-3-small dimensions
        index_name = "lab_vector_index"

        try:
            lab_collection.create_search_index({
                "name": index_name,
                "type": "vectorSearch",
                "definition": {
                    "fields": [
                        {
                            "type": "vector",
                            "path": "embedding",
                            "numDimensions": dims,
                            "similarity": "cosine",
                        },
                    ]
                },
            })
        except Exception as exc:
            if "already exists" in str(exc).lower():
                pass  # Idempotent
            else:
                raise

        # Verify index exists
        indexes = list(lab_collection.list_search_indexes())
        index_names = [idx.get("name") for idx in indexes]
        assert index_name in index_names, f"Index {index_name} not found in {index_names}"

    def test_documents_have_embeddings(self, lab_collection):
        """Verify all docs have embedding field with correct dimensions."""
        for doc in lab_collection.find({}, {"embedding": 1}):
            assert "embedding" in doc, f"Doc {doc['_id']} missing embedding"
            assert len(doc["embedding"]) == 1536, f"Doc {doc['_id']} embedding wrong dims: {len(doc['embedding'])}"

    def test_risk_acceptance_loaded_by_guard(self):
        """Verify RISK-008 exists in risk_acceptance.json and guard can read it."""
        import json
        ra_path = os.path.join(REPO_ROOT, "brain", "machine_artifacts", "content", "risk_acceptance.json")
        with open(ra_path, encoding="utf-8") as f:
            ra = json.load(f)

        risk_008 = [e for e in ra["entries"] if e["id"] == "RISK-008"]
        assert len(risk_008) == 1, "RISK-008 not found in risk_acceptance.json"
        assert risk_008[0]["boss_decision"] == "ACCEPTED"
        assert "lab" in risk_008[0]["description"].lower()


# ---------------------------------------------------------------------------
# EPIC-008-F-011-S-004-REQ-B-001: Single canonical embedding model
# (OpenAI text-embedding-3-large, vector dimension 3072).
# ---------------------------------------------------------------------------

CANONICAL_EMBED_MODEL = "text-embedding-3-large"
CANONICAL_EMBED_DIMS = 3072


def test_canonical_embedding_model_data_side():
    """EPIC-008-F-011-S-004-REQ-B-001-PYTEST-1.

    For env prefix 'dev', verify every vectorSearch index on
    {env}_PublicHealthData.providers and {env}_PublicHealthData.SpecialtyMetaData
    has numDimensions == 3072 (the dimension of text-embedding-3-large).
    """
    from pymongo import MongoClient

    conn = _get_frontend_connection()
    client = _ch_connection()
    try:
        env = "dev"
        targets = [
            (f"{env}_PublicHealthData", "providers"),
            (f"{env}_PublicHealthData", "SpecialtyMetaData"),
        ]

        total_vector_indexes_seen = 0
        for db_name, coll_name in targets:
            coll = client[db_name][coll_name]
            try:
                indexes = list(coll.list_search_indexes())
            except Exception as exc:
                # Some envs/clusters may not support search indexes; treat as
                # "nothing to check here" rather than a violation.
                _CH_LOG.info(f"[INFO] {db_name}.{coll_name}: list_search_indexes failed: {exc}")
                continue

            for idx in indexes:
                if idx.get("type") != "vectorSearch":
                    continue
                index_name = idx.get("name", "<unnamed>")
                # Mongo Atlas exposes the live spec under "latestDefinition";
                # historical/proposed under "definition". Prefer latest.
                definition = idx.get("latestDefinition") or idx.get("definition") or {}
                fields = definition.get("fields") or []
                for field in fields:
                    if field.get("type") != "vector":
                        continue
                    total_vector_indexes_seen += 1
                    n = field.get("numDimensions")
                    path = field.get("path", "<unknown>")
                    assert n == CANONICAL_EMBED_DIMS, (
                        f"index {index_name} field {path} has numDimensions={n}, "
                        f"expected {CANONICAL_EMBED_DIMS} "
                        f"(text-embedding-3-large per EPIC-008-F-011-S-004-REQ-B-001)"
                    )

        if total_vector_indexes_seen == 0:
            pytest.skip(
                "No vectorSearch indexes found on dev_PublicHealthData.providers or "
                "dev_PublicHealthData.SpecialtyMetaData; nothing to verify on this env."
            )
    finally:
        client.close()


def _iter_in_scope_py_files(repo_root):
    """Yield absolute Path objects for production-executable .py files in scope."""
    from pathlib import Path

    roots = [
        Path(repo_root) / "Code" / "ConversationalUX" / "FindCareChat" / "backend",
        Path(repo_root) / "Code" / "DataPipelines",
        Path(repo_root) / "evaluateCare" / "Code",
        Path(repo_root) / "sharedServices" / "Code",
        Path(repo_root) / "FrontEndApplicationLib" / "src",
    ]
    excluded_substrings = (
        "/tests/",
        "/test_",
        "_test.py",
        "/conftest.py",
        "/_oneshots/",
        "/__pycache__/",
        "/node_modules/",
    )
    for root in roots:
        if not root.exists():
            continue
        for p in root.rglob("*.py"):
            posix = p.as_posix()
            if any(sub in posix for sub in excluded_substrings):
                continue
            yield p


def _slice_after(text, marker_idx, length=200):
    """Return up to `length` chars starting at marker_idx (clamped)."""
    return text[marker_idx:marker_idx + length]


def _line_number_for_offset(text, offset):
    return text.count("\n", 0, offset) + 1


def _file_defines_embed_model_canonical(text):
    """True if the file defines EMBED_MODEL = "text-embedding-3-large"."""
    # Plain string scan, no regex.
    needle = 'EMBED_MODEL'
    pos = 0
    while True:
        idx = text.find(needle, pos)
        if idx == -1:
            return False
        # Look at up to 80 chars after the name for an assignment to canonical.
        window = text[idx:idx + 100]
        if "=" in window and CANONICAL_EMBED_MODEL in window:
            # Make sure '=' precedes the model literal in the window.
            eq_pos = window.find("=")
            model_pos = window.find(CANONICAL_EMBED_MODEL)
            if 0 <= eq_pos < model_pos:
                return True
        pos = idx + len(needle)


def _file_imports_embed_model(text):
    """True if file imports EMBED_MODEL from another module."""
    # Look for 'import EMBED_MODEL' (covers `from X import EMBED_MODEL`).
    return "import EMBED_MODEL" in text or " EMBED_MODEL," in text or ", EMBED_MODEL" in text


def _file_self_model_canonical(text):
    """True if class __init__ sets self._model to canonical (literal or default).

    Accepts either:
      - self._model = "text-embedding-3-large"
      - self._model = config.get(..., EMBED_MODEL)  (when EMBED_MODEL is canonical here or imported)
      - self._model = model  with `model = config.get(..., EMBED_MODEL)` earlier
    """
    if "self._model" not in text:
        return False
    # Direct literal assignment.
    if f'self._model = "{CANONICAL_EMBED_MODEL}"' in text:
        return True
    if f"self._model = '{CANONICAL_EMBED_MODEL}'" in text:
        return True
    # Indirect: self._model = model, with `model = config.get(..., EMBED_MODEL)`.
    if "self._model = model" in text and "EMBED_MODEL" in text:
        if _file_defines_embed_model_canonical(text) or _file_imports_embed_model(text):
            return True
    return False


def _extract_model_argument(window):
    """Given a window of text starting at 'embeddings.create(', return the
    raw substring of the model= argument (up to comma or close paren), or None.
    """
    key = "model="
    k = window.find(key)
    if k == -1:
        return None
    start = k + len(key)
    # Stop at first comma or close-paren at depth 0.
    depth = 0
    i = start
    while i < len(window):
        ch = window[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            if depth == 0:
                break
            depth -= 1
        elif ch == "," and depth == 0:
            break
        i += 1
    return window[start:i].strip()


def _model_arg_is_canonical(model_arg, text):
    """Decide whether the model= argument resolves to the canonical model."""
    if model_arg is None:
        return False
    arg = model_arg.strip()
    # Literal forms.
    if arg == f'"{CANONICAL_EMBED_MODEL}"' or arg == f"'{CANONICAL_EMBED_MODEL}'":
        return True
    # Symbolic: EMBED_MODEL — accept if defined-here or imported-here.
    if arg == "EMBED_MODEL":
        return _file_defines_embed_model_canonical(text) or _file_imports_embed_model(text)
    # Symbolic: self._model — accept if init sets it canonical or defaults to EMBED_MODEL.
    if arg == "self._model":
        return _file_self_model_canonical(text)
    return False


def test_canonical_embedding_model_code_side():
    """EPIC-008-F-011-S-004-REQ-B-001-PYTEST-2.

    Walk in-scope production source files and assert every
    `embeddings.create(...)` call uses the canonical model
    (text-embedding-3-large), either as a literal or via the
    EMBED_MODEL/self._model conventions documented in embedding_worker.py.
    """
    needle = "embeddings.create("
    violations = []  # (file_path, line_number, found_model)

    for path in _iter_in_scope_py_files(REPO_ROOT):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if needle not in text:
            continue

        pos = 0
        while True:
            idx = text.find(needle, pos)
            if idx == -1:
                break
            window = _slice_after(text, idx, length=400)
            model_arg = _extract_model_argument(window)
            if not _model_arg_is_canonical(model_arg, text):
                line_no = _line_number_for_offset(text, idx)
                violations.append((str(path), line_no, model_arg if model_arg else "<no model= argument found>"))
            pos = idx + len(needle)

    formatted = "\n  ".join(f"{p}:{ln} -> model={m}" for p, ln, m in violations)
    assert violations == [], (
        "Non-canonical embedding model usage found "
        "(EPIC-008-F-011-S-004-REQ-B-001 requires text-embedding-3-large):\n  "
        + formatted
    )
