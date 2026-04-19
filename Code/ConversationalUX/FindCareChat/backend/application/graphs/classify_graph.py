# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# classify_graph.py — LangGraph StateGraph for the /classify FastAPI handler.
#
# Wraps the original handler body in nodes:
#   1) safety_gate  — IP lock + emergency detection (BUG-SAFETY-001)
#   2) classify     — vector specialty search + location parse + homeo cache
# A conditional edge routes a halt from safety_gate straight to END so the
# emergency response shape is preserved exactly.

from typing import Any, Optional
from typing_extensions import TypedDict  # langgraph schema introspection requires this on Py<3.12

from langgraph.graph import StateGraph, START, END


class ClassifyState(TypedDict, total=False):
    # ── inputs ──
    message: str
    ip: str
    # ── intermediate ──
    halt: bool                    # True if safety_gate produced the response
    # ── output ──
    response: Any                 # final dict returned to caller


def build_classify_graph(
    safety_service,
    specialty_service,
    get_db_fn,
    env_prefix: str,
    emergency_response: str,
):
    """Build and compile the /classify graph.

    All arguments are the same module-level service refs that the original
    handler closed over; passing them in keeps the graph testable.
    """

    def safety_gate(state: ClassifyState) -> dict:
        """BUG-SAFETY-001: emergency detection on every user input."""
        ip = state.get("ip", "unknown")
        msg = state.get("message", "")
        if safety_service.is_ip_locked(ip):
            return {"halt": True,
                    "response": {"emergency": True, "response": emergency_response}}
        if safety_service.is_emergency(msg):
            full_history = [{"role": "user", "content": msg}]
            safety_service.lock_ip(ip, trigger_message=msg, history=full_history)
            return {"halt": True,
                    "response": {"emergency": True, "response": emergency_response}}
        return {"halt": False}

    def safety_router(state: ClassifyState) -> str:
        return "end" if state.get("halt") else "classify"

    def classify_node(state: ClassifyState) -> dict:
        """Vector search for specialties + simple location parse + homeo cache.
        Mirrors the original handler body line-for-line."""
        msg = state.get("message", "")

        # Vector search for specialties
        result = specialty_service.find_specialty_codes(msg)

        if "error" in result:
            return {"response": {"specialties": [], "error": result["error"]}}

        # Map to the format the frontend expects
        specialties = [
            {"code": s["Code"], "name": s["Display Name"],
             "can_prescribe": s.get("can_prescribe", False),
             "homeopathic": s.get("homeopathic", False),
             "rank": s.get("rank", 0)}
            for s in result.get("specialties", [])
        ]

        # Simple location extraction — no LLM needed
        msg_lower = msg.lower()
        state_code: Optional[str] = None
        city = None
        county = None
        # State codes
        for code in ["DE", "MS", "VA", "IN"]:
            if f" {code.lower()} " in f" {msg_lower} " or msg_lower.endswith(f" {code.lower()}"):
                state_code = code
                break
        # Full state names
        state_names = {"delaware": "DE", "mississippi": "MS", "virginia": "VA", "indiana": "IN"}
        for name, code in state_names.items():
            if name in msg_lower:
                state_code = code
                break

        # Load homeopathic generalists for client-side cache
        homeo_generalists = []
        try:
            db = get_db_fn()
            if db:
                for doc in db[f"{env_prefix}_PublicHealthData"]["SpecialtyMetaData"].find(
                    {"homeopathic_general": True, "Section": "Individual"},
                    {"_id": 0, "Code": 1, "Display Name": 1, "can_prescribe": 1, "homeopathic": 1}
                ):
                    homeo_generalists.append({
                        "code": doc.get("Code", ""),
                        "name": doc.get("Display Name", ""),
                        "can_prescribe": doc.get("can_prescribe", False),
                        "homeopathic": True,
                        "homeopathic_general": True,
                    })
        except Exception:
            pass

        return {"response": {
            "specialties": specialties,
            "homeopathic_generalists": homeo_generalists,
            "state": state_code,
            "city": city,
            "county": county,
            "model": "text-embedding-3-large (vector search)",
        }}

    g = StateGraph(ClassifyState)
    g.add_node("safety_gate", safety_gate)
    g.add_node("classify", classify_node)
    g.add_edge(START, "safety_gate")
    g.add_conditional_edges("safety_gate", safety_router, {
        "end": END,
        "classify": "classify",
    })
    g.add_edge("classify", END)
    return g.compile()
