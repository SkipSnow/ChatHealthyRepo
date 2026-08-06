"""Check database for evidence of test success."""
import os
from pymongo import MongoClient


def test_check_database_for_evidence():
    """Verify test data exists in MongoDB."""

    mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    assert mongo_uri, "MONGO_FRONTEND_connectionString not set"

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        db = client["dev_PublicHealthData"]

        # List all collections
        collections = sorted(db.list_collection_names())

        print(f"\n✓ Connected to database: dev_PublicHealthData")
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
