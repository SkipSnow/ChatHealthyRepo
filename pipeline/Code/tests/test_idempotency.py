# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""PipelineWorkerBase.output_exists_and_valid tests — PIPE-ID-001"""
import os, sys, pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# We can't instantiate the ABC directly, so create a minimal concrete subclass
from pipeline_worker_base import PipelineWorkerBase

class _TestWorker(PipelineWorkerBase):
    def _pipeline_process(self): pass
    def _pipeline_row_key(self): return ""
    def _pipeline_resume(self): pass
    def _pipeline_has_next(self): return False
    def _pipeline_build_result(self): return {}

class MockCollection:
    def __init__(self, count):
        self._count = count
        self.full_name = "test_db.test_collection"
    def count_documents(self, query):
        return self._count

def test_output_exists():
    """EPIC-006-F-014-S-001: returns True when count >= min_count"""
    worker = _TestWorker({})
    assert worker.output_exists_and_valid(MockCollection(100), min_count=50) is True

def test_no_output():
    """EPIC-006-F-014-S-001: returns False when empty"""
    worker = _TestWorker({})
    assert worker.output_exists_and_valid(MockCollection(0), min_count=1) is False

def test_insufficient_count():
    """EPIC-006-F-014-S-001: returns False when count < min_count"""
    worker = _TestWorker({})
    assert worker.output_exists_and_valid(MockCollection(5), min_count=100) is False
