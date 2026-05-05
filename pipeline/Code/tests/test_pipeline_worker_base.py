# Copyright © 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

"""Tests for PipelineWorkerBase — Template Method base class.

Covers:
- Abstract method enforcement (TypeError at instantiation)
- Lifecycle: _pipeline_open → loop → _pipeline_close always called
- fail_on_row_error=False: continues, accumulates errors, calls _pipeline_resume
- fail_on_row_error=True: abends on first row error
- row_errors structure: {row_key, reason}
- _pipeline_on_job_failure called on fatal exception
- _pipeline_close called even when job fails fatally
- _pipeline_build_result called only on success
"""

import sys
import os
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pipeline_worker_base import PipelineWorkerBase


# ── Test double ───────────────────────────────────────────────────────────────

class _Worker(PipelineWorkerBase):
    """Minimal compliant subclass.  Drives through a list of string items.
    Items equal to FAIL_SENTINEL raise ValueError.
    """

    FAIL_SENTINEL = "__FAIL__"

    def __init__(self, config, items):
        super().__init__(config)
        self._items = list(items)
        self._cursor = -1
        self._current = None

        # Observation flags
        self.opened = False
        self.closed = False
        self.resumed_count = 0
        self.job_failure_exc = None
        self.build_result_called = False

    def _pipeline_open(self):
        self.opened = True

    def _pipeline_close(self):
        self.closed = True

    def _pipeline_on_job_failure(self, exc):
        self.job_failure_exc = exc

    def _pipeline_has_next(self):
        self._cursor += 1
        if self._cursor >= len(self._items):
            return False
        self._current = self._items[self._cursor]
        return True

    def _pipeline_process(self):
        if self._current == self.FAIL_SENTINEL:
            raise ValueError(f"Bad item: {self._current}")

    def _pipeline_row_key(self):
        return str(self._current)

    def _pipeline_resume(self):
        self.resumed_count += 1

    def _pipeline_build_result(self):
        self.build_result_called = True
        return {"items": self._items, "errors": self.row_errors}


# ── Compliance: abstract method enforcement ───────────────────────────────────

def test_missing_process_raises_type_error():
    class Bad(PipelineWorkerBase):
        def _pipeline_has_next(self): return False
        def _pipeline_row_key(self): return ""
        def _pipeline_resume(self): pass
        def _pipeline_build_result(self): return {}

    with pytest.raises(TypeError):
        Bad({})


def test_missing_has_next_raises_type_error():
    class Bad(PipelineWorkerBase):
        def _pipeline_process(self): pass
        def _pipeline_row_key(self): return ""
        def _pipeline_resume(self): pass
        def _pipeline_build_result(self): return {}

    with pytest.raises(TypeError):
        Bad({})


def test_missing_row_key_raises_type_error():
    class Bad(PipelineWorkerBase):
        def _pipeline_process(self): pass
        def _pipeline_has_next(self): return False
        def _pipeline_resume(self): pass
        def _pipeline_build_result(self): return {}

    with pytest.raises(TypeError):
        Bad({})


def test_missing_resume_raises_type_error():
    class Bad(PipelineWorkerBase):
        def _pipeline_process(self): pass
        def _pipeline_has_next(self): return False
        def _pipeline_row_key(self): return ""
        def _pipeline_build_result(self): return {}

    with pytest.raises(TypeError):
        Bad({})


def test_missing_build_result_raises_type_error():
    class Bad(PipelineWorkerBase):
        def _pipeline_process(self): pass
        def _pipeline_has_next(self): return False
        def _pipeline_row_key(self): return ""
        def _pipeline_resume(self): pass

    with pytest.raises(TypeError):
        Bad({})


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def test_open_and_close_called_on_success():
    w = _Worker({}, ["a", "b"])
    w.pipeline_execute()
    assert w.opened
    assert w.closed


def test_close_called_on_job_failure():
    """_pipeline_close must be called even when the job fails fatally."""
    w = _Worker({"fail_on_row_error": True}, [_Worker.FAIL_SENTINEL])
    with pytest.raises(ValueError):
        w.pipeline_execute()
    assert w.closed


def test_build_result_not_called_on_job_failure():
    w = _Worker({"fail_on_row_error": True}, [_Worker.FAIL_SENTINEL])
    with pytest.raises(ValueError):
        w.pipeline_execute()
    assert not w.build_result_called


def test_build_result_called_on_success():
    w = _Worker({}, ["a"])
    w.pipeline_execute()
    assert w.build_result_called


# ── fail_on_row_error=False (default) ─────────────────────────────────────────

def test_continues_after_row_error():
    w = _Worker({}, ["a", _Worker.FAIL_SENTINEL, "b"])
    result = w.pipeline_execute()
    assert len(result["errors"]) == 1


def test_row_errors_structure():
    w = _Worker({}, [_Worker.FAIL_SENTINEL])
    result = w.pipeline_execute()
    assert len(result["errors"]) == 1
    err = result["errors"][0]
    assert set(err.keys()) == {"row_key", "reason"}
    assert err["row_key"] == _Worker.FAIL_SENTINEL
    assert "Bad item" in err["reason"]


def test_resume_called_after_row_error():
    w = _Worker({}, [_Worker.FAIL_SENTINEL, _Worker.FAIL_SENTINEL])
    w.pipeline_execute()
    assert w.resumed_count == 2


def test_resume_not_called_when_no_errors():
    w = _Worker({}, ["a", "b", "c"])
    w.pipeline_execute()
    assert w.resumed_count == 0


def test_multiple_errors_accumulated():
    items = [_Worker.FAIL_SENTINEL] * 5
    w = _Worker({}, items)
    result = w.pipeline_execute()
    assert len(result["errors"]) == 5


def test_on_job_failure_not_called_on_row_error():
    """Row errors are not fatal when fail_on_row_error=False."""
    w = _Worker({}, [_Worker.FAIL_SENTINEL])
    w.pipeline_execute()
    assert w.job_failure_exc is None


# ── fail_on_row_error=True ────────────────────────────────────────────────────

def test_abends_on_first_row_error():
    w = _Worker({"fail_on_row_error": True}, ["a", _Worker.FAIL_SENTINEL, "b"])
    with pytest.raises(ValueError, match="Bad item"):
        w.pipeline_execute()


def test_error_still_logged_when_fail_on_row_error(caplog):
    import logging
    w = _Worker({"fail_on_row_error": True}, [_Worker.FAIL_SENTINEL])
    with pytest.raises(ValueError):
        with caplog.at_level(logging.ERROR):
            w.pipeline_execute()
    assert any("Row error" in r.message for r in caplog.records)


def test_row_errors_populated_before_raise():
    """row_errors must contain the failing row even when the job abends."""
    w = _Worker({"fail_on_row_error": True}, [_Worker.FAIL_SENTINEL])
    with pytest.raises(ValueError):
        w.pipeline_execute()
    assert len(w.row_errors) == 1


def test_on_job_failure_called_on_abend():
    w = _Worker({"fail_on_row_error": True}, [_Worker.FAIL_SENTINEL])
    with pytest.raises(ValueError):
        w.pipeline_execute()
    assert w.job_failure_exc is not None


# ── Fatal error in _pipeline_open ────────────────────────────────────────────

def test_fatal_open_error_calls_on_job_failure_and_close():
    class FailOpen(_Worker):
        def _pipeline_open(self):
            raise RuntimeError("blob unavailable")

    w = FailOpen({}, [])
    with pytest.raises(RuntimeError, match="blob unavailable"):
        w.pipeline_execute()
    assert w.job_failure_exc is not None
    assert w.closed


# ── Empty input ───────────────────────────────────────────────────────────────

def test_empty_input_succeeds():
    w = _Worker({}, [])
    result = w.pipeline_execute()
    assert result["errors"] == []
    assert w.opened
    assert w.closed
    assert w.resumed_count == 0
