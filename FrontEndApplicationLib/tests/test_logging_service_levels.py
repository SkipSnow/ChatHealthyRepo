# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""All 6 log levels of ChatHealthyLoggingService.

Realizes EPIC-008-F-002-S-011-REQ-T-001 — every level (debug, info,
warning, error, critical, exception) on ChatHealthyLoggingService MUST
emit a record at the matching stdlib logging level, route through the
typed exc= path on REQ-B-007, and honor the if_not_debug_log gate on
REQ-B-009.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException


LEVELS = [
    ("debug",     logging.DEBUG),
    ("info",      logging.INFO),
    ("warning",   logging.WARNING),
    ("error",     logging.ERROR),
    ("critical",  logging.CRITICAL),
    ("exception", logging.ERROR),
]


@pytest.fixture
def file_log(tmp_path, monkeypatch):
    """Bind the logger to a per-test file at DEBUG; yield the file path.
    Returns the path so each test can read what was written."""
    monkeypatch.setenv("CH_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("CH_LOG_DESTINATION", str(tmp_path))
    monkeypatch.setenv("CH_COMPONENT", "test_logging_service_levels")

    import chathealthy_frontend_lib.logging_service as svc
    svc._bound_destination = None
    svc._bound_level = None

    log_path = tmp_path / "test_logging_service_levels.log"
    yield log_path

    for h in list(logging.getLogger().handlers):
        try:
            h.close()
        finally:
            logging.getLogger().removeHandler(h)
    svc._bound_destination = None
    svc._bound_level = None


@pytest.mark.parametrize("method_name,expected_level", LEVELS)
def test_level_emits_at_correct_level(file_log, method_name, expected_level):
    log = ChatHealthyLoggingService()
    method = getattr(log, method_name)
    method("hello from %s", method_name, if_not_debug_log=True)
    for h in logging.getLogger().handlers:
        h.flush()
    body = file_log.read_text(encoding="utf-8")
    assert f"hello from {method_name}" in body
    assert logging.getLevelName(expected_level) in body


@pytest.mark.parametrize("method_name,expected_level", LEVELS)
def test_level_accepts_typed_exc(file_log, method_name, expected_level):
    log = ChatHealthyLoggingService()
    method = getattr(log, method_name)
    che = ChatHealthyException(
        mode="test_mode",
        message="test message",
        component="TestComponent",
    )
    method("level-%s with exc", method_name, exc=che, if_not_debug_log=True)
    for h in logging.getLogger().handlers:
        h.flush()
    body = file_log.read_text(encoding="utf-8")
    assert f"level-{method_name} with exc" in body
    assert "ChatHealthyException(mode='test_mode'" in body
    assert "component='TestComponent'" in body


@pytest.mark.parametrize("method_name,expected_level", LEVELS)
def test_level_rejects_non_chathealthy_exc(file_log, method_name, expected_level):
    log = ChatHealthyLoggingService()
    method = getattr(log, method_name)
    with pytest.raises(ChatHealthyException) as excinfo:
        method("non-typed exc", exc=ValueError("nope"), if_not_debug_log=True)
    assert excinfo.value.mode == "logger_exception_not_chathealthy"


@pytest.mark.parametrize("method_name,expected_level", LEVELS)
def test_level_gated_when_not_forced_and_level_above_debug(
    tmp_path, monkeypatch, method_name, expected_level,
):
    """if_not_debug_log defaults to False so the call returns silently
    when the configured level is above DEBUG. REQ-B-009 — chatty calls
    are suppressed in non-debug mode."""
    monkeypatch.setenv("CH_LOG_LEVEL", "INFO")
    monkeypatch.setenv("CH_LOG_DESTINATION", str(tmp_path))
    monkeypatch.setenv("CH_COMPONENT", "gated_test")

    import chathealthy_frontend_lib.logging_service as svc
    svc._bound_destination = None
    svc._bound_level = None

    log_path = tmp_path / "gated_test.log"
    log = ChatHealthyLoggingService()
    method = getattr(log, method_name)
    method("gated-%s", method_name)
    for h in logging.getLogger().handlers:
        h.flush()

    body = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
    assert f"gated-{method_name}" not in body

    for h in list(logging.getLogger().handlers):
        try:
            h.close()
        finally:
            logging.getLogger().removeHandler(h)
    svc._bound_destination = None
    svc._bound_level = None


def test_exception_method_attaches_active_exception_without_exc_kwarg(file_log):
    """exception() without exc= falls back to sys.exc_info() per the
    library's contract — verifies the bare .exception(...) call path is
    operational alongside the typed exc= path."""
    log = ChatHealthyLoggingService()
    try:
        raise ValueError("active-exception-marker")
    except ValueError:
        log.exception("caught: %s", "active-exception-marker", if_not_debug_log=True)
    for h in logging.getLogger().handlers:
        h.flush()
    body = file_log.read_text(encoding="utf-8")
    assert "caught: active-exception-marker" in body
    assert "ValueError" in body
    assert "active-exception-marker" in body
