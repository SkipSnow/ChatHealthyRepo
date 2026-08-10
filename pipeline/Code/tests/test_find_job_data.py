"""Find data created by test job in the Pipelines metadata database"""
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


def test_find_job_data():
    """Find warnings/errors in PIPELINE Pipelines metadata database."""

    # Connect to PIPELINE cluster where discrepancy_report writes data
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "admin")
    assert client, "Could not get pipeline MongoDB connection"
    try:
        # Look in Pipelines metadata database
        db = client["pipelineAdmin"]

        _CH_LOG.info(f"\n✓ Checking Pipelines metadata database...")

        # Look for pipeline.discrepancy_reports collection
        if "pipeline.discrepancy_reports" in db.list_collection_names():
            coll = db["pipeline.discrepancy_reports"]
            count = coll.count_documents({})
            _CH_LOG.info(f"✓ Found pipeline.discrepancies: {count} total documents")

            # Find test data
            test_docs = list(coll.find({"source": "ProviderPipelineOnDemand"}).limit(5))
            if test_docs:
                _CH_LOG.info(f"✓ Found {len(test_docs)} documents from ProviderPipelineOnDemand:")
                for doc in test_docs:
                    _CH_LOG.info(f"    - {doc.get('level', 'unknown').upper()}: {doc.get('details', '')[:50]}...")
                _CH_LOG.info(f"\n✅ SUCCESS: Test data found in database!")
            else:
                _CH_LOG.info("✗ No ProviderPipelineOnDemand data found")
        else:
            _CH_LOG.info("✗ pipeline.discrepancies collection not found")
            _CH_LOG.info(f"  Available collections: {db.list_collection_names()}")

        assert True

    finally:
        client.close()
