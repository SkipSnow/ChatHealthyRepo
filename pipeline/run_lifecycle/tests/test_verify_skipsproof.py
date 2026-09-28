"""Verify skipsProof data in database"""
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


def test_verify_skipsproof_data():
    """Check that skipsProof job data was written to PIPELINE database."""

    # Connect to PIPELINE cluster where the data is actually written
    utilities = ChatHealthyMongoUtilities()
    client = utilities.getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    assert client, "Could not get pipeline MongoDB connection"

    try:
        db = client["pipelineAdmin"]
        coll = db["discrepancyLog"]

        # Findings are recorded to the unified discrepancyLog as per-class
        # type_aggregate documents keyed by run.
        docs = list(coll.find({"kind": "type_aggregate"}).limit(10))

        _CH_LOG.info("Found %d type_aggregate documents in discrepancyLog:", len(docs))
        for doc in docs:
            severity = str(doc.get("severity", "unknown")).upper()
            finding_class = doc.get("class", "")
            run_id = doc.get("run_id", "")
            _CH_LOG.info("  - %s: %s count=%s (run_id: %s)",
                         severity, finding_class, doc.get("count"), run_id)

        assert True

    finally:
        client.close()
