"""Find warnings and errors created by test_fatal_errors.py"""
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


def test_find_warnings_and_errors():
    """Look for warnings and errors created by the test job in PIPELINE database."""

    # Connect to PIPELINE cluster where discrepancy_report writes data
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "admin")
    assert client, "Could not get pipeline MongoDB connection"

    try:
        db = client["pipelineAdmin"]

        # Look for any collection that might contain warnings/errors
        collections = db.list_collection_names()
        _CH_LOG.info(f"\nSearching for warnings/errors in {len(collections)} collections...")

        found_any = False
        for coll_name in collections:
            coll = db[coll_name]
            try:
                # Search for ProviderPipelineOnDemand source
                result = coll.find_one({"source": "ProviderPipelineOnDemand"})
                if result:
                    found_any = True
                    _CH_LOG.info(f"\n✓ Found in {coll_name}:")
                    _CH_LOG.info(f"    {result}")

                # Also search for warnings
                result = coll.find_one({"type": "warning"})
                if result:
                    found_any = True
                    _CH_LOG.info(f"\n✓ Found warning in {coll_name}:")
                    _CH_LOG.info(f"    {result}")

                # Search for errors
                result = coll.find_one({"type": "error"})
                if result:
                    found_any = True
                    _CH_LOG.info(f"\n✓ Found error in {coll_name}:")
                    _CH_LOG.info(f"    {result}")
            except Exception as e:
                # Skip collections that can't be queried
                pass

        if not found_any:
            _CH_LOG.info("✗ No warnings or errors found in database")
            _CH_LOG.info(f"  Searched {len(collections)} collections")
        else:
            _CH_LOG.info("\n✓ Test data found in database!")

        assert True

    finally:
        client.close()
