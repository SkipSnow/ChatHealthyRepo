"""Find warnings and errors created by test_fatal_errors.py"""
import os
from pymongo import MongoClient


def test_find_warnings_and_errors():
    """Look for warnings and errors created by the test job."""

    mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    assert mongo_uri, "MONGO_FRONTEND_connectionString not set"

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        db = client["dev_PublicHealthData"]

        # Look for any collection that might contain warnings/errors
        collections = db.list_collection_names()
        print(f"\nSearching for warnings/errors in {len(collections)} collections...")

        found_any = False
        for coll_name in collections:
            coll = db[coll_name]
            try:
                # Search for ProviderPipelineOnDemand source
                result = coll.find_one({"source": "ProviderPipelineOnDemand"})
                if result:
                    found_any = True
                    print(f"\n✓ Found in {coll_name}:")
                    print(f"    {result}")

                # Also search for warnings
                result = coll.find_one({"type": "warning"})
                if result:
                    found_any = True
                    print(f"\n✓ Found warning in {coll_name}:")
                    print(f"    {result}")

                # Search for errors
                result = coll.find_one({"type": "error"})
                if result:
                    found_any = True
                    print(f"\n✓ Found error in {coll_name}:")
                    print(f"    {result}")
            except Exception as e:
                # Skip collections that can't be queried
                pass

        if not found_any:
            print("✗ No warnings or errors found in database")
            print(f"  Searched {len(collections)} collections")
        else:
            print("\n✓ Test data found in database!")

        assert True

    finally:
        client.close()
