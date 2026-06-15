# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# test_chat_graph.py — Tests for LangGraph chat orchestration port.
#
# Verifies: same inputs produce same outputs as the old sequential flow.
# All services are mocked — this tests the graph wiring, not the services.

import sys
import os
import pytest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from application.chat_graph import build_graph


def make_graph(
    safety_emergency=False,
    safety_ip_locked=False,
    safety_admin_unlock=False,
    llm_response="Here are providers in Virginia",
    llm_stop_reason="end",
    llm_tokens_in=100,
    llm_tokens_out=50,
    search_term="bone docs",
):
    """Factory: build a graph with configurable mock behavior."""
    safety = MagicMock()
    safety.try_admin_unlock.return_value = safety_admin_unlock
    safety.is_ip_locked.return_value = safety_ip_locked
    safety.is_emergency.return_value = safety_emergency

    llm_mock = MagicMock(return_value={
        "content": llm_response,
        "stop_reason": llm_stop_reason,
        "usage": {"input_tokens": llm_tokens_in, "output_tokens": llm_tokens_out},
    })

    debug_logger = MagicMock()

    graph = build_graph(
        safety_service=safety,
        prompt_maker=MagicMock(build_system_prompt=lambda **kw: "system prompt"),
        tool_router=MagicMock(),
        anthropic_tools=[],
        llm_chat_fn=llm_mock,
        extract_search_term_fn=lambda m: search_term,
        strip_redundant_summary_fn=lambda t, tc, pc: t,
        build_summary_message_fn=lambda **kw: "summary",
        format_chat_history_fn=lambda m, truncate=True: m,
        debug_logger=debug_logger,
        emergency_response="CALL 911",
        chat_model="gpt-4.1",
    )
    return graph, llm_mock, safety, debug_logger


class TestNormalPath:
    """Normal chat flow — no emergency, no lock, LLM called."""

    def test_returns_llm_response(self):
        graph, llm_mock, _, _ = make_graph()
        result = graph.invoke({"user_message": "bone doc in VA", "history": [], "ip": "1.2.3.4"})
        assert result["final_response"]["response"] == "Here are providers in Virginia"

    def test_returns_token_counts(self):
        graph, _, _, _ = make_graph(llm_tokens_in=200, llm_tokens_out=75)
        result = graph.invoke({"user_message": "bone doc", "history": [], "ip": "1.2.3.4"})
        assert result["final_response"]["tokens_in"] == 200
        assert result["final_response"]["tokens_out"] == 75

    def test_llm_is_called(self):
        graph, llm_mock, _, _ = make_graph()
        graph.invoke({"user_message": "bone doc", "history": [], "ip": "1.2.3.4"})
        assert llm_mock.called

    def test_search_term_extracted(self):
        graph, _, _, _ = make_graph(search_term="shrinks")
        result = graph.invoke({"user_message": "find me shrinks", "history": [], "ip": "1.2.3.4"})
        assert result["search_term"] == "shrinks"

    def test_safety_checked(self):
        graph, _, safety, _ = make_graph()
        graph.invoke({"user_message": "bone doc", "history": [], "ip": "1.2.3.4"})
        safety.is_emergency.assert_called_once_with("bone doc")

    def test_debug_logger_called(self):
        graph, _, _, debug_logger = make_graph()
        graph.invoke({"user_message": "bone doc", "history": [], "ip": "1.2.3.4"})
        debug_logger.log_chat.assert_called_once()


class TestEmergencyPath:
    """Emergency detected — LLM must NOT be called."""

    def test_returns_emergency_response(self):
        graph, _, _, _ = make_graph(safety_emergency=True)
        result = graph.invoke({"user_message": "chest pain", "history": [], "ip": "1.2.3.4"})
        assert result["final_response"]["response"] == "CALL 911"

    def test_emergency_flag_set(self):
        graph, _, _, _ = make_graph(safety_emergency=True)
        result = graph.invoke({"user_message": "chest pain", "history": [], "ip": "1.2.3.4"})
        assert result["final_response"]["emergency"] is True

    def test_llm_not_called(self):
        graph, llm_mock, _, _ = make_graph(safety_emergency=True)
        graph.invoke({"user_message": "chest pain", "history": [], "ip": "1.2.3.4"})
        assert not llm_mock.called

    def test_ip_locked_after_emergency(self):
        graph, _, safety, _ = make_graph(safety_emergency=True)
        graph.invoke({"user_message": "chest pain", "history": [], "ip": "1.2.3.4"})
        safety.lock_ip.assert_called_once()


class TestIPLockedPath:
    """IP already locked — LLM must NOT be called."""

    def test_returns_emergency_response(self):
        graph, _, _, _ = make_graph(safety_ip_locked=True)
        result = graph.invoke({"user_message": "hello", "history": [], "ip": "1.2.3.4"})
        assert result["final_response"]["response"] == "CALL 911"

    def test_llm_not_called(self):
        graph, llm_mock, _, _ = make_graph(safety_ip_locked=True)
        graph.invoke({"user_message": "hello", "history": [], "ip": "1.2.3.4"})
        assert not llm_mock.called


class TestAdminUnlockPath:
    """Admin unlock command — LLM must NOT be called."""

    def test_returns_unlock_message(self):
        graph, _, _, _ = make_graph(safety_admin_unlock=True)
        result = graph.invoke({"user_message": "unlock", "history": [], "ip": "1.2.3.4"})
        assert result["final_response"]["response"] == "Session unlocked."

    def test_llm_not_called(self):
        graph, llm_mock, _, _ = make_graph(safety_admin_unlock=True)
        graph.invoke({"user_message": "unlock", "history": [], "ip": "1.2.3.4"})
        assert not llm_mock.called


class TestParallelExecution:
    """Verify pre-processing runs in parallel (both safety and search_term produce results)."""

    def test_both_safety_and_search_term_resolve(self):
        graph, _, _, _ = make_graph(search_term="bone docs")
        result = graph.invoke({"user_message": "bone doc in VA", "history": [], "ip": "1.2.3.4"})
        assert result["is_emergency"] is False
        assert result["search_term"] == "bone docs"

    def test_post_processing_runs(self):
        graph, _, _, _ = make_graph()
        result = graph.invoke({"user_message": "bone doc", "history": [], "ip": "1.2.3.4"})
        # URL guard ran (response_text should be set)
        assert result["response_text"] is not None
