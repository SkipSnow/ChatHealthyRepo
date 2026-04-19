# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# graphs package — one LangGraph StateGraph per business-logic FastAPI handler
# in main.py. Each module exports a build_<handler>_graph(...) factory that
# returns a compiled graph; main.py calls <graph>.invoke({...}) inside the
# handler body, satisfying engineering rule v4-043.
#
# No new business logic lives here. Each graph is a thin wrapper around the
# pre-existing service code, preserving inputs and outputs exactly.

from .search_graph import build_search_graph
from .classify_graph import build_classify_graph
from .qa_report_graph import build_qa_report_graph
from .qa_report_submit_graph import build_qa_report_submit_graph
from .welcome_graph import build_welcome_graph

__all__ = [
    "build_search_graph",
    "build_classify_graph",
    "build_qa_report_graph",
    "build_qa_report_submit_graph",
    "build_welcome_graph",
]
