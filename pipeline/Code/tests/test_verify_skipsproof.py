"""Verify skipsProof data in database"""
import os
from pymongo import MongoClient


def test_verify_skipsproof_data():
    """Check that skipsProof job data was written to database."""

    mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    assert mongo_uri, "MONGO_FRONTEND_connectionString not set"

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        db = client["chathealthypipelines"]
        coll = db["pipeline.discrepancy_reports"]

        # Find skipsProof data
        docs = list(coll.find({"source": "skipsProof"}).limit(10))

        print(f"\n✅ SUCCESS: Found {len(docs)} documents from skipsProof job:")
        for doc in docs:
            level = doc.get("level", "unknown").upper()
            details = doc.get("details", "")[:60]
            run_id = doc.get("run_id", "")
            print(f"  - {level}: {details}... (run_id: {run_id})")

        assert len(docs) > 0, "No skipsProof data found"

    finally:
        client.close()
