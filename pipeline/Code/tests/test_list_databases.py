"""List all databases on the cluster."""
import os
from pymongo import MongoClient


def test_list_databases():
    """Show all databases and their collections."""

    mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString")
    assert mongo_uri, "MONGO_FRONTEND_connectionString not set"

    client = MongoClient(mongo_uri, serverSelectionTimeoutMS=5000)
    try:
        # List all databases
        databases = client.list_database_names()

        print(f"\n✓ Databases on cluster: {len(databases)}")
        for db_name in sorted(databases)[:20]:
            db = client[db_name]
            colls = db.list_collection_names()
            if colls:
                print(f"\n  {db_name} ({len(colls)} collections):")
                for coll in colls[:5]:
                    count = db[coll].count_documents({})
                    print(f"    - {coll}: {count} docs")

        assert True, "Database listing successful"

    finally:
        client.close()
