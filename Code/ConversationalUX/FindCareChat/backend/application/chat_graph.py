# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# chat_graph.py — LangGraph orchestration for /chat endpoint.
#
# Straight port of the sequential flow in main.py into a parallel graph.
# No new business logic. Same inputs, same outputs, same prompts.
#
# Graph shape:
#   USER INPUT
#       │
#   ┌───┴───┐
#   │ GATE  │  ← does this need an LLM?
#   └───┬───┘
#    NO/ \YES
#    │    │
#  direct ├── safety_check ──┐
#  handler├── search_term ───┤  (parallel)
#         └── (future nodes)─┘
#              │
#         ┌────┴────┐
#         │  MERGE  │  ← halt? downgrade?
#         └────┬────┘
#              │
#         ┌────┴────┐
#         │  CHAT   │  ← existing tool loop, untouched
#         └────┬────┘
#              │
#         ┌────┴────┐
#         │POST GATE│  ← what needs post-processing?
#         └────┬────┘
#              │
#         ├── url_guard ─────┐
#         ├── strip_summary ─┤  (conditional parallel)
#         └──────────────────┘
#              │
#         RESPONSE

import json
import logging
import os
import re
from typing import Any, Optional, Annotated
from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END

_log = logging.getLogger("findcare.graph")


# ---------------------------------------------------------------------------
# Reducer: when parallel nodes both write the same key, keep the last value.
# ---------------------------------------------------------------------------
def _last_value(existing, new):
    """Reducer for parallel fan-out: last write wins."""
    return new


# ---------------------------------------------------------------------------
# Graph state — everything flows through this dict
# ---------------------------------------------------------------------------
class ChatState(TypedDict, total=False):
    # --- inputs ---
    user_message: Annotated[str, _last_value]
    history: Annotated[list, _last_value]
    ip: Annotated[str, _last_value]
    # --- pre-processing results ---
    is_emergency: Annotated[bool, _last_value]
    is_ip_locked: Annotated[bool, _last_value]
    admin_unlock: Annotated[bool, _last_value]
    search_term: Annotated[str, _last_value]
    needs_llm: Annotated[bool, _last_value]
    # --- chat loop results ---
    response_text: Annotated[str, _last_value]
    total_in: Annotated[int, _last_value]
    total_out: Annotated[int, _last_value]
    last_provider_result: Annotated[Optional[dict], _last_value]
    last_trials_result: Annotated[Optional[dict], _last_value]
    loop_iterations: Annotated[int, _last_value]
    # --- post-processing results ---
    pagination: Annotated[Any, _last_value]
    trials_meta: Annotated[Any, _last_value]
    # --- final ---
    final_response: Annotated[Any, _last_value]
    error: Annotated[Optional[str], _last_value]
    error_type: Annotated[Optional[str], _last_value]


# ---------------------------------------------------------------------------
# Node factory — takes service refs, returns node functions
# ---------------------------------------------------------------------------
def build_graph(
    safety_service,
    url_guardian,
    prompt_maker,
    tool_router,
    anthropic_tools,
    llm_chat_fn,
    extract_search_term_fn,
    strip_redundant_summary_fn,
    build_summary_message_fn,
    format_chat_history_fn,
    debug_logger,
    emergency_response: str,
    chat_model: str,
):
    """Build and compile the LangGraph chat graph.

    All business logic lives in the existing services — this just wires
    the execution order and parallelism.
    """

    # ── GATE: does this need an LLM? ──────────────────────────────
    def gate_node(state: ChatState) -> dict:
        """For now, everything that hits /chat needs an LLM.
        The direct handler path (pagination, filter toggles) is already
        handled by /search and /classify endpoints — they never reach here.
        This node exists as the hook for future deterministic routing."""
        return {"needs_llm": True}

    def gate_router(state: ChatState) -> str:
        if not state.get("needs_llm", True):
            return "direct_handler"
        return "fan_out"

    # ── PRE-PROCESSING: parallel nodes ────────────────────────────
    # IMPORTANT: parallel nodes must only return keys they OWN.
    # Returning {**state, ...} would cause the last-finishing node
    # to overwrite keys set by the other parallel node.

    def safety_check(state: ChatState) -> dict:
        """Safety: admin unlock, IP lock, emergency detection."""
        msg = state["user_message"]
        ip = state.get("ip", "unknown")

        if safety_service.try_admin_unlock(msg, ip):
            return {"admin_unlock": True, "is_ip_locked": False, "is_emergency": False}
        if safety_service.is_ip_locked(ip):
            return {"is_ip_locked": True, "is_emergency": False, "admin_unlock": False}
        if safety_service.is_emergency(msg):
            full_history = list(state.get("history", [])) + [{"role": "user", "content": msg}]
            safety_service.lock_ip(ip, trigger_message=msg, history=full_history)
            return {"is_emergency": True, "is_ip_locked": False, "admin_unlock": False}

        return {"is_emergency": False, "is_ip_locked": False, "admin_unlock": False}

    def search_term_extract(state: ChatState) -> dict:
        """Extract colloquial search term from user message."""
        term = extract_search_term_fn(state["user_message"])
        return {"search_term": term}

    # ── MERGE: check pre-processing results ───────────────────────

    def merge_node(state: ChatState) -> dict:
        """Check safety results. If halt, short-circuit."""
        return {}

    def merge_router(state: ChatState) -> str:
        if state.get("admin_unlock"):
            return "early_exit"
        if state.get("is_ip_locked") or state.get("is_emergency"):
            return "early_exit"
        return "chat_loop"

    # ── EARLY EXIT ────────────────────────────────────────────────

    def early_exit(state: ChatState) -> dict:
        """Build response for safety halts and admin unlock."""
        if state.get("admin_unlock"):
            return {"final_response": {"response": "Session unlocked."}}
        # Emergency or IP locked
        return {"final_response": {
            "response": emergency_response,
            "emergency": True,
        }}

    # ── CHAT LOOP: the existing tool loop, untouched ──────────────

    def chat_loop(state: ChatState) -> dict:
        """Existing chat loop from main.py — straight port, no changes."""
        user_msg = state["user_message"]
        history = state.get("history", [])

        user_msg_count = sum(1 for m in history if m.get("role") == "user")
        system = prompt_maker.build_system_prompt(
            emergency_response=emergency_response,
            follow_up_check=user_msg_count > 0 and user_msg_count % 5 == 0,
        )
        messages = list(history) + [{"role": "user", "content": user_msg}]
        total_in = total_out = 0

        _log.info("CHAT model=%s call=initial msgs=%d", chat_model, len(messages))
        response = llm_chat_fn(chat_model, messages, tools=anthropic_tools, system=system, max_tokens=4096)
        total_in += response.get("usage", {}).get("input_tokens", 0)
        total_out += response.get("usage", {}).get("output_tokens", 0)

        loop_iter = 0
        last_provider_result = None
        last_trials_result = None

        while response.get("stop_reason") in ("tool_calls", "tool_use"):
            tool_calls = response.get("tool_calls", [])
            if not tool_calls:
                break
            tool_results = tool_router.handle_normalized_tool_calls(
                tool_calls, messages, format_chat_history_fn)

            for i, tc in enumerate(tool_calls):
                tc_name = tc["function"]["name"]
                if tc_name == "find_providers":
                    try:
                        last_provider_result = json.loads(tool_results[i]["content"])
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass
                elif tc_name == "search_clinical_trials":
                    try:
                        last_trials_result = json.loads(tool_results[i]["content"])
                    except (json.JSONDecodeError, KeyError, IndexError):
                        pass

            assistant_msg = {"role": "assistant", "content": response.get("content", "")}
            if tool_calls:
                assistant_msg["tool_calls"] = [
                    {"id": tc["id"], "type": "function", "function": tc["function"]}
                    for tc in tool_calls
                ]
            messages.append(assistant_msg)
            for tr in tool_results:
                messages.append(tr)

            loop_iter += 1
            _log.info("CHAT tool_loop iter=%d tools=%s", loop_iter,
                      [tc["function"]["name"] for tc in tool_calls])
            response = llm_chat_fn(chat_model, messages, tools=anthropic_tools,
                                   system=system, max_tokens=4096)
            total_in += response.get("usage", {}).get("input_tokens", 0)
            total_out += response.get("usage", {}).get("output_tokens", 0)

        text = response.get("content", "")

        return {
            "response_text": text,
            "total_in": total_in,
            "total_out": total_out,
            "last_provider_result": last_provider_result,
            "last_trials_result": last_trials_result,
            "loop_iterations": loop_iter,
        }

    # ── POST-PROCESSING GATE ──────────────────────────────────────

    def post_gate(state: ChatState) -> dict:
        """Determine what post-processing is needed."""
        return {}

    def post_router(state: ChatState) -> list[str]:
        """Return list of post-processing nodes to run."""
        nodes = ["url_guard"]  # always run URL guardian

        # Strip summary only when pagination summary exists
        if state.get("last_provider_result"):
            pr = state["last_provider_result"]
            if isinstance(pr, dict) and pr.get("total_count", 0) > 0:
                nodes.append("build_pagination")

        if state.get("last_trials_result"):
            tr = state["last_trials_result"]
            if isinstance(tr, dict) and tr.get("trials"):
                nodes.append("build_trials")

        return nodes

    # ── POST-PROCESSING NODES ─────────────────────────────────────

    def url_guard(state: ChatState) -> dict:
        """URL Guardian — validate URLs in response text."""
        text = state.get("response_text", "")
        text = url_guardian.guard_text(text)
        text = re.sub(r'(?<!\[)Skip Snow on LinkedIn(?!\])',
                      '[Skip Snow on LinkedIn](https://linkedin.com/in/skipsnow)', text)
        return {"response_text": text}

    def build_pagination(state: ChatState) -> dict:
        """Build pagination metadata and strip redundant summary."""
        pr = state.get("last_provider_result", {})
        if not pr or not isinstance(pr, dict):
            return {}

        total_count = pr.get("total_count", 0)
        if total_count <= 0:
            return {}

        search_term = state.get("search_term", state["user_message"])
        user_summary = build_summary_message_fn(
            has_more=pr.get("has_more", False),
            total_count=total_count,
            page_count=pr.get("count", 0),
            specialty_searched=search_term,
            specialization_options=pr.get("specialization_options"),
            state=pr.get("state", ""),
            city=(pr.get("search_params") or {}).get("city", ""),
            county=(pr.get("search_params") or {}).get("county", ""),
        )

        pagination = {
            "has_more": pr.get("has_more", False),
            "first_npi": pr.get("first_npi"),
            "last_npi": pr.get("last_npi"),
            "count": pr.get("count", 0),
            "total_count": total_count,
            "page_start": pr.get("page_start", 1),
            "page_end": pr.get("page_end", 0),
            "search_params": pr.get("search_params"),
            "specialization_options": pr.get("specialization_options"),
            "summary_message": user_summary,
        }

        result = {"pagination": pagination}

        # Strip redundant summary from LLM text
        text = state.get("response_text", "")
        if user_summary and text:
            result["response_text"] = strip_redundant_summary_fn(text, total_count, pr.get("count", 0))

        return result

    def build_trials(state: ChatState) -> dict:
        """Build trials metadata."""
        import requests as _requests_lib
        tr = state.get("last_trials_result", {})
        if not tr or not isinstance(tr, dict):
            return {}

        trial_list = tr.get("trials", [])
        if not trial_list:
            return {}

        trial_count = len(trial_list)
        has_travel = any(t.get("travel_info") for t in trial_list)
        search_term = state.get("search_term", state["user_message"])

        parts = [f"Found {trial_count} recruiting trial{'s' if trial_count != 1 else ''} for '{search_term}'."]
        if has_travel:
            parts.append(" Travel times included.")
        parts.append(
            f" [View all on ClinicalTrials.gov]"
            f"(https://clinicaltrials.gov/search?cond={_requests_lib.utils.quote(search_term)}&aggFilters=status:rec)"
        )

        trials_meta = {
            "trial_count": trial_count,
            "condition": search_term,
            "summary_message": "".join(parts),
        }
        return {"trials_meta": trials_meta}

    # ── DIRECT HANDLER (future) ───────────────────────────────────

    def direct_handler(state: ChatState) -> dict:
        """Placeholder for deterministic handling without LLM.
        Today: everything that reaches /chat needs an LLM.
        Tomorrow: route structured queries here."""
        return {"final_response": {"response": "Direct handler not yet implemented."}}

    # ── FINAL ASSEMBLY ────────────────────────────────────────────

    def assemble_response(state: ChatState) -> dict:
        """Build the final ChatResponse dict."""
        text = state.get("response_text", "")
        result = {
            "response": text,
            "tokens_in": state.get("total_in"),
            "tokens_out": state.get("total_out"),
        }
        if state.get("pagination"):
            result["pagination"] = state["pagination"]
        if state.get("trials_meta"):
            result["trials"] = state["trials_meta"]

        # Log
        debug_logger.log_chat(
            state.get("ip", "unknown"),
            state["user_message"],
            len(state.get("history", [])),
            state.get("loop_iterations", 0),
            state.get("total_in"),
            state.get("total_out"),
            text,
            None,
        )

        _log.info("CHAT complete tokens_in=%d tokens_out=%d pagination=%s trials=%s",
                   state.get("total_in", 0), state.get("total_out", 0),
                   bool(state.get("pagination")), bool(state.get("trials_meta")))

        return {"final_response": result}

    # ── PARALLEL FAN-OUT NODE ─────────────────────────────────────
    # LangGraph needs a single node to fan out from. This node does
    # nothing — it just exists so we can draw parallel edges from it.

    def pre_fan_out(state: ChatState) -> dict:
        return {}

    def post_fan_out(state: ChatState) -> dict:
        return {}

    # ── BUILD THE GRAPH ───────────────────────────────────────────

    graph = StateGraph(ChatState)

    # Add nodes
    graph.add_node("gate", gate_node)
    graph.add_node("pre_fan_out", pre_fan_out)
    graph.add_node("safety_check", safety_check)
    graph.add_node("search_term_extract", search_term_extract)
    graph.add_node("merge", merge_node)
    graph.add_node("early_exit", early_exit)
    graph.add_node("chat_loop", chat_loop)
    graph.add_node("url_guard", url_guard)
    graph.add_node("build_pagination", build_pagination)
    graph.add_node("build_trials", build_trials)
    graph.add_node("post_fan_out", post_fan_out)
    graph.add_node("direct_handler", direct_handler)
    graph.add_node("assemble_response", assemble_response)

    # ── EDGES ─────────────────────────────────────────────────────

    # Entry → gate
    graph.add_edge(START, "gate")

    # Gate → direct handler OR pre-processing fan-out
    graph.add_conditional_edges("gate", gate_router, {
        "direct_handler": "direct_handler",
        "fan_out": "pre_fan_out",
    })

    # Parallel pre-processing: fan_out → both nodes simultaneously
    graph.add_edge("pre_fan_out", "safety_check")
    graph.add_edge("pre_fan_out", "search_term_extract")

    # Both converge at merge
    graph.add_edge("safety_check", "merge")
    graph.add_edge("search_term_extract", "merge")

    # Merge decides: early exit or chat loop
    graph.add_conditional_edges("merge", merge_router, {
        "early_exit": "early_exit",
        "chat_loop": "chat_loop",
    })

    # Early exit → END
    graph.add_edge("early_exit", END)

    # Chat loop → post-processing fan-out
    graph.add_edge("chat_loop", "post_fan_out")

    # Parallel post-processing: always url_guard, conditionally pagination/trials
    graph.add_edge("post_fan_out", "url_guard")
    graph.add_edge("post_fan_out", "build_pagination")
    graph.add_edge("post_fan_out", "build_trials")

    # All post-processing → assemble
    graph.add_edge("url_guard", "assemble_response")
    graph.add_edge("build_pagination", "assemble_response")
    graph.add_edge("build_trials", "assemble_response")

    # Direct handler → END
    graph.add_edge("direct_handler", END)

    # Assemble → END
    graph.add_edge("assemble_response", END)

    return graph.compile()
