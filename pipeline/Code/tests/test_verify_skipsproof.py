"""Verify skipsProof data in database"""
import os
import sys

sys.path.insert(0, "FrontEndApplicationLib/src")
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


def test_verify_skipsproof_data():
    """Check that skipsProof job data was written to PIPELINE database."""

    # Connect to PIPELINE cluster where the data is actually written
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "admin")
    assert client, "Could not get pipeline MongoDB connection"

    try:
        db = client["pipelineAdmin"]
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
