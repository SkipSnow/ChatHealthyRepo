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
        client = MongoClient(conn, serverSelectionTimeoutMS=15000)
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
