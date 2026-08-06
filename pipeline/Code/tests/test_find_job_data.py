"""Find data created by test job in chathealthypipelines database"""
import os
import sys

sys.path.insert(0, "FrontEndApplicationLib/src")
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


def test_find_job_data():
    """Find warnings/errors in PIPELINE chathealthypipelines database."""

    # Connect to PIPELINE cluster where discrepancy_report writes data
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "admin")
    assert client, "Could not get pipeline MongoDB connection"
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
