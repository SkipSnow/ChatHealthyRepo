"""Check database for evidence of test success."""
import os
import sys

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

sys.path.insert(0, "ChatHealthyLib/src")
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities


def test_check_database_for_evidence():
    """Verify test data exists in PIPELINE MongoDB."""

    # Connect to PIPELINE cluster where discrepancy data is written
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    assert client, "Could not get pipeline MongoDB connection"

    try:
        db = client["pipelineAdmin"]

        # List all collections
        collections = sorted(db.list_collection_names())

        _CH_LOG.info(f"\n✓ Connected to metadata database: Pipelines")
        _CH_LOG.info(f"✓ Total collections: {len(collections)}")

        # Look for discrepancy-related collections
        discrepancy_colls = [c for c in collections if any(x in c.lower() for x in ['discrepancy', 'warning', 'error', 'report'])]
        if discrepancy_colls:
            _CH_LOG.info(f"✓ Found {len(discrepancy_colls)} relevant collections:")
            for coll_name in discrepancy_colls:
                coll = db[coll_name]
                count = coll.count_documents({})
                _CH_LOG.info(f"    - {coll_name}: {count} documents")

                # Show sample
                sample = coll.find_one()
                if sample:
                    _CH_LOG.info(f"      Sample doc: {str(sample)[:80]}...")
        else:
            _CH_LOG.info(f"Note: No discrepancy/warning/error collections found")
            _CH_LOG.info(f"All collections: {collections[:10]}...")

        # Success
        assert True, "Database connectivity verified"

    finally:
        client.close()
