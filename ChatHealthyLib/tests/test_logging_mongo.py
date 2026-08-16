# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Unit test: ChatHealthyLoggingService logging to MongoDB."""

from __future__ import annotations

import os
import uuid
from chathealthy_lib.logging_service import ChatHealthyLoggingService, set_mongo_log_identity
from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


def test_mongo_logging_requires_identity():
    """Test: enabling mongo logging without setting identity raises exception."""
    os.environ["CH_LOG_DESTINATION"] = "mongo"
    os.environ["CH_LOG_LEVEL"] = "INFO"
    os.environ["ENV_PREFIX"] = "dev"
    os.environ["CH_SPACE_NAME"] = "test_component"

    import chathealthy_lib.logging_service as svc
    svc._bound_destinations = None
    svc._bound_level = None
    svc._mongo_log_identity = None

    try:
        log = ChatHealthyLoggingService()
        log.info("This should fail")
        assert False, "Should have raised ChatHealthyException"
    except ChatHealthyException as e:
        assert e.mode == "mongo_log_identity_not_set"
        _CH_LOG.info("✓ Correctly requires identity before mongo logging")


def test_mongo_logging_and_verify():
    """Test: set identity, log to mongo, verify message in database."""
    os.environ["CH_LOG_DESTINATION"] = "mongo"
    os.environ["CH_LOG_LEVEL"] = "INFO"
    os.environ["ENV_PREFIX"] = "dev"
    os.environ["CH_SPACE_NAME"] = "test_component"

    import chathealthy_lib.logging_service as svc
    svc._bound_destinations = None
    svc._bound_level = None
    svc._mongo_log_identity = None

    set_mongo_log_identity("pipelineEditor")

    log = ChatHealthyLoggingService()
    test_id = str(uuid.uuid4())
    test_message = f"Test message: {test_id}"

    log.info(test_message)

    # Verify message made it to database
    utilities = ChatHealthyMongoUtilities()
    conn = utilities.getConnection("pipelineEditor", "ChatHealthyFrontEnd")
    db = conn["Pipelines"]
    coll = db["Log"]

    found = coll.find_one({"formatted": {"$regex": test_id}})
    assert found is not None, f"Message not found in database: {test_message}"
    assert test_id in found["formatted"]

    coll.delete_one({"_id": found["_id"]})
    _CH_LOG.info("✓ Successfully logged to mongo and verified in database")


if __name__ == "__main__":
    test_mongo_logging_requires_identity()
    test_mongo_logging_and_verify()
    _CH_LOG.info("\n✓ All mongo logging tests passed")
