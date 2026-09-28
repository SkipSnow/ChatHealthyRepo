"""Find data created by test job in the Pipelines metadata database"""
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


def test_find_job_data():
    """Find warnings/errors in PIPELINE Pipelines metadata database."""

    # Connect to PIPELINE cluster where discrepancy_report writes data
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    assert client, "Could not get pipeline MongoDB connection"
    try:
        # Look in Pipelines metadata database
        db = client["pipelineAdmin"]

        _CH_LOG.info("Checking Pipelines metadata database...")

        # Findings live in the unified discrepancyLog collection as two kinds:
        # per-record `record` docs and per-class `type_aggregate` docs.
        if "discrepancyLog" in db.list_collection_names():
            coll = db["discrepancyLog"]
            count = coll.count_documents({})
            _CH_LOG.info("Found discrepancyLog: %d total documents", count)

            aggregates = list(coll.find({"kind": "type_aggregate"}).limit(5))
            for doc in aggregates:
                _CH_LOG.info("    - %s %s: count=%s",
                             str(doc.get("severity", "")).upper(),
                             doc.get("class", ""), doc.get("count"))
        else:
            _CH_LOG.info("discrepancyLog collection not found")
            _CH_LOG.info("  Available collections: %s", db.list_collection_names())

        assert True

    finally:
        client.close()
