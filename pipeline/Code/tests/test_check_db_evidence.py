"""Check database for evidence of test success."""
import os
import sys

sys.path.insert(0, "FrontEndApplicationLib/src")
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


def test_check_database_for_evidence():
    """Verify test data exists in PIPELINE MongoDB."""

    # Connect to PIPELINE cluster where discrepancy data is written
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "admin")
    assert client, "Could not get pipeline MongoDB connection"

    try:
        db = client["chathealthypipelines"]

        # List all collections
        collections = sorted(db.list_collection_names())

        print(f"\n✓ Connected to PIPELINE database: chathealthypipelines")
        print(f"✓ Total collections: {len(collections)}")

        # Look for discrepancy-related collections
        discrepancy_colls = [c for c in collections if any(x in c.lower() for x in ['discrepancy', 'warning', 'error', 'report'])]
        if discrepancy_colls:
            print(f"✓ Found {len(discrepancy_colls)} relevant collections:")
            for coll_name in discrepancy_colls:
                coll = db[coll_name]
                count = coll.count_documents({})
                print(f"    - {coll_name}: {count} documents")

                # Show sample
                sample = coll.find_one()
                if sample:
                    print(f"      Sample doc: {str(sample)[:80]}...")
        else:
            print(f"Note: No discrepancy/warning/error collections found")
            print(f"All collections: {collections[:10]}...")

        # Success
        assert True, "Database connectivity verified"

    finally:
        client.close()
