# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""EPIC-003-F-003-S-001 — the ChatHealthyException class capabilities the
other suites do not assert. Modes, server, component, context and the
wrapped original are asserted by test_llm_facade.py; these are the rest."""
import pytest

from chathealthy_lib.exceptions import ChatHealthyException


# EPIC-003-F-003-S-001-REQ-B-002-TEST-1
def test_the_message_is_carried_for_the_log_record():
    exc = ChatHealthyException(mode="m", message="what happened, for the log")
    assert exc.message == "what happened, for the log"
    assert str(exc) == "what happened, for the log"


# EPIC-003-F-003-S-001-REQ-B-007-TEST-1
def test_the_stack_is_captured_at_construction():
    def a_place_with_a_name():
        return ChatHealthyException(mode="m", message="x")
    exc = a_place_with_a_name()
    assert "a_place_with_a_name" in exc.construction_stack
    assert "__init__" not in exc.construction_stack.splitlines()[-1]


# EPIC-003-F-003-S-001-REQ-B-008-TEST-1
def test_catchable_as_an_ordinary_exception():
    with pytest.raises(Exception):
        raise ChatHealthyException(mode="m", message="x")
    try:
        raise ChatHealthyException(mode="m", message="x")
    except Exception as exc:
        assert isinstance(exc, ChatHealthyException)


# EPIC-003-F-003-S-001-REQ-B-009-TEST-1
def test_the_repr_states_the_whole_failure():
    original = ValueError("the wrapped one")
    exc = ChatHealthyException(
        mode="m", message="msg", server="find_care", component="UM",
        exception=original, call_site="here", attempts=3)
    rendered = repr(exc)
    for piece in ("mode='m'", "message='msg'", "server='find_care'",
                  "component='UM'", "call_site='here'", "attempts=3",
                  "the wrapped one"):
        assert piece in rendered
