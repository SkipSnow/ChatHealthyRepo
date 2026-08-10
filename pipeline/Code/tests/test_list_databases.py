"""List all databases on the PIPELINE cluster."""
import os
import sys

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

sys.path.insert(0, "FrontEndApplicationLib/src")
from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities


def test_list_databases():
    """Show all databases and their collections on PIPELINE cluster."""

    # Connect to PIPELINE cluster
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "admin")
    assert client, "Could not get pipeline MongoDB connection"
    try:
        # List all databases
        databases = client.list_database_names()

        _CH_LOG.info(f"\n✓ Databases on cluster: {len(databases)}")
        for db_name in sorted(databases)[:20]:
            db = client[db_name]
            colls = db.list_collection_names()
            if colls:
                _CH_LOG.info(f"\n  {db_name} ({len(colls)} collections):")
                for coll in colls[:5]:
                    count = db[coll].count_documents({})
                    _CH_LOG.info(f"    - {coll}: {count} docs")

        assert True, "Database listing successful"

    finally:
        client.close()
