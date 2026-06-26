"""Stub compliance test for EPIC-008-F-002-S-010-REQ-B-009.

Pattern:
  - Configure the canonical ChatHealthyLoggingService to DEBUG, redirected to a
    pytest-owned log destination.
  - Call TimedCollection.find() with batch_size=K against a fake/stub coll.
  - Read the configured log destination.
  - Assert the mongo.find START line includes batch_size=K verbatim in opts.
  - Repeat with batch_size omitted; assert START line opts does NOT include
    batch_size.

This is the stub. The body is intentionally a TODO so we have a failing test
attached to the REQ — and so this REQ cannot be marked done until the test
actually exercises the library and reads the log.
"""

import pytest


@pytest.mark.skip(reason="stub for REQ-B-009 — implement before release")
def test_find_forwards_explicit_batch_size_to_pymongo_and_logs_it() -> None:
    raise NotImplementedError(
        "REQ-B-009 compliance test stub. Implementation MUST: "
        "(1) point ChatHealthyLoggingService at a temp log file at DEBUG level; "
        "(2) wrap a stub Collection in TimedCollection; "
        "(3) call .find(filter={}, batch_size=1000); "
        "(4) read the temp log; "
        "(5) assert 'batch_size' and '1000' both appear in the captured "
        "'mongo.find START' line's opts portion."
    )


@pytest.mark.skip(reason="stub for REQ-B-009 — implement before release")
def test_find_without_batch_size_does_not_log_or_forward_one() -> None:
    raise NotImplementedError(
        "REQ-B-009 compliance test stub. Implementation MUST: "
        "(1) point ChatHealthyLoggingService at a temp log file at DEBUG level; "
        "(2) wrap a stub Collection in TimedCollection; "
        "(3) call .find(filter={}) WITHOUT batch_size; "
        "(4) read the temp log; "
        "(5) assert 'batch_size' does NOT appear in the captured "
        "'mongo.find START' line's opts portion."
    )
