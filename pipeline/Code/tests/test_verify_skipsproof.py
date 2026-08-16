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
        coll = db["pipeline.discrepancy_reports"]

        # Find skipsProof data
        docs = list(coll.find({"source": "skipsProof"}).limit(10))

        _CH_LOG.info(f"\n✅ SUCCESS: Found {len(docs)} documents from skipsProof job:")
        for doc in docs:
            level = doc.get("level", "unknown").upper()
            details = doc.get("details", "")[:60]
            run_id = doc.get("run_id", "")
            _CH_LOG.info(f"  - {level}: {details}... (run_id: {run_id})")

        assert len(docs) > 0, "No skipsProof data found"

    finally:
        client.close()
