"""List all databases on the PIPELINE cluster."""
import os
import sys

sys.path.insert(0, "FrontEndApplicationLib/src")
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


def test_list_databases():
    """Show all databases and their collections on PIPELINE cluster."""

    # Connect to PIPELINE cluster
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor")
    assert client, "Could not get pipeline MongoDB connection"
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
