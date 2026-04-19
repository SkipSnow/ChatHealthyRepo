# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
#
# Studio visualization entry point. Constructs _chat_graph with mocked
# service dependencies so `langgraph dev` can render the graph structure
# without spinning up the real backend (mTLS, MongoDB, OpenAI, etc.).
#
# The graph TOPOLOGY (nodes + edges + state shape) is identical to the
# production graph in main.py — only the service callables are stubbed.
# This makes the file safe to use for Studio viewing AND keeps the
# visualization in lock-step with the real graph definition in
# chat_graph.py.
#
# Per v4-043 / ARCH-GRAPH-ORCHESTRATION-001-REQ-T002.

from __future__ import annotations

from unittest.mock import MagicMock

# langgraph.json declares dependencies path as the backend dir, so
# `application` is importable as a top-level package when Studio loads us.
from application.chat_graph import build_graph


_chat_graph = build_graph(
    safety_service=MagicMock(name="safety_service"),
    url_guardian=MagicMock(name="url_guardian"),
    prompt_maker=MagicMock(name="prompt_maker"),
    tool_router=MagicMock(name="tool_router"),
    anthropic_tools=[],
    llm_chat_fn=lambda *a, **kw: "",
    extract_search_term_fn=lambda *a, **kw: "",
    strip_redundant_summary_fn=lambda *a, **kw: "",
    build_summary_message_fn=lambda *a, **kw: "",
    format_chat_history_fn=lambda *a, **kw: [],
    debug_logger=MagicMock(name="debug_logger"),
    emergency_response="",
    chat_model="gpt-4.1",
)
