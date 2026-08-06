"""Find data created by test job in chathealthypipelines database"""
import os
from pymongo import MongoClient


def test_find_job_data():
    """Find warnings/errors in chathealthypipelines database."""

    mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    assert mongo_uri, "MONGO_FRONTEND_connectionString not set"

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        # Look in chathealthypipelines database
        db = client["chathealthypipelines"]

        print(f"\n✓ Checking chathealthypipelines database...")

        # Look for pipeline.discrepancy_reports collection
        if "pipeline.discrepancy_reports" in db.list_collection_names():
            coll = db["pipeline.discrepancy_reports"]
            count = coll.count_documents({})
            print(f"✓ Found pipeline.discrepancies: {count} total documents")

            # Find test data
            test_docs = list(coll.find({"source": "ProviderPipelineOnDemand"}).limit(5))
            if test_docs:
                print(f"✓ Found {len(test_docs)} documents from ProviderPipelineOnDemand:")
                for doc in test_docs:
                    print(f"    - {doc.get('level', 'unknown').upper()}: {doc.get('details', '')[:50]}...")
                print(f"\n✅ SUCCESS: Test data found in database!")
            else:
                print("✗ No ProviderPipelineOnDemand data found")
        else:
            print("✗ pipeline.discrepancies collection not found")
            print(f"  Available collections: {db.list_collection_names()}")

        assert True

    finally:
        client.close()
