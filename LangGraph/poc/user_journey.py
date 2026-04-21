# FindCare — user journey (POC for LangGraph Studio).
#
# Principles (Skip 2026-04-20):
#   - The graph IS the user journey.
#   - Every box is a decision point OR a business-meaningful action.
#     No plumbing nodes.
#   - Edges carry the meaning (sequential, parallel, conditional).
#   - ip_lock_check gates the session BEFORE auth.
#   - Every user message fans out to 3 safety checks + classifier in parallel.
#   - Classifier picks: stop_the_flow / invoke_tool_X / clarify / utterance.
#   - Every tool / clarify / utterance pops back to the top (user_utterance)
#     for the next user turn.
#   - Filter clicks + Evaluate clicks are CLIENT-SIDE — not in this graph.
#   - Only `evaluate_handoff` is a hard terminal.

from typing_extensions import TypedDict
from langgraph.graph import StateGraph, START, END


# ══════════════════════════════════════════════════════════════
# State
# ══════════════════════════════════════════════════════════════

class JourneyState(TypedDict, total=False):
    # Input
    message: str
    history: list

    # Session-gate flags
    ip_locked: bool
    authorized: bool

    # Per-utterance safety flags (set by parallel detectors)
    is_emergency: bool
    is_repetitive: bool

    # Classifier output
    intent: str  # stop | clarify | clinical_advice | speak_to_us |
                 # unknown | about_skip | about_chathealthy |
                 # care_giver | clinical_trial | evaluate

    # Tool outputs
    location: dict
    specialty_query: dict
    specialties: list
    homeopathic_expansion: list
    providers: list
    trials: list

    # System utterance dispatch
    utterance_type: str  # ip_locked | unauthorized | welcome |
                         # emergency | repetitive | clarify |
                         # clinical_advice | lead_captured |
                         # unknown_logged | about_skip |
                         # about_chathealthy | specialties |
                         # trials | general
    response: dict


# ══════════════════════════════════════════════════════════════
# Session gate (BEFORE auth)
# ══════════════════════════════════════════════════════════════

# ip_lock_check_session removed — the single per-turn ip_lock_check node
# below handles every turn including the first.


# ══════════════════════════════════════════════════════════════
# Authorization
# ══════════════════════════════════════════════════════════════

def get_authorization(state: JourneyState) -> dict:
    """UNBUILT LAYER. Triggered after lockout is clear (initially or after
    unlock-key / expiry). Verifies + sets authorized flag and the
    appropriate utterance_type."""
    authorized = True  # stub
    out = {"authorized": authorized,
           "utterance_type": "welcome" if authorized else "unauthorized"}
    return out


# ══════════════════════════════════════════════════════════════
# User utterance (the top of the loop)
# ══════════════════════════════════════════════════════════════

def user_utterance(state: JourneyState) -> dict:
    """USER ACTOR. Any user message — first or subsequent. Fans out to
    the 3 parallel safety detectors and the classifier."""
    return {}


# ══════════════════════════════════════════════════════════════
# Per-utterance parallel safety + classifier
# ══════════════════════════════════════════════════════════════

def emergency_detector(state: JourneyState) -> dict:
    """Deterministic, parallel. Keyword match."""
    is_em = False
    out = {"is_emergency": is_em}
    if is_em:
        out["utterance_type"] = "emergency"
    return out

def ip_lock_check(state: JourneyState) -> dict:
    """Self-contained DB lookup. Sets ip_locked + utterance_type."""
    locked = False
    out = {"ip_locked": locked}
    if locked:
        out["utterance_type"] = "ip_locked"
    return out

def lock_route(state: JourneyState):
    """ip_lock_check has THREE outcomes (internal attempt-counting +
    timer hidden inside the node):
       - locked, give up      → user_exits (END)
       - locked, allow attempt → user_utterance (loop to retry)
       - not locked            → get_authorization (then parallel + merge + tools)"""
    if state.get("ip_locked"):
        if state.get("lock_attempts_exhausted"):
            return "user_exits"
        return "user_utterance"
    return "get_authorization"

def repetitive_detector(state: JourneyState) -> dict:
    """Deterministic, parallel."""
    rep = False
    out = {"is_repetitive": rep}
    if rep:
        out["utterance_type"] = "repetitive"
    return out

def classifier(state: JourneyState) -> dict:
    """LLM, parallel. Outputs an intent that determines the next action:
       stop_the_flow / clarify / a specific tool / a direct utterance."""
    return {"intent": "care_giver"}  # stub


# ══════════════════════════════════════════════════════════════
# Merge + main routing decision
# ══════════════════════════════════════════════════════════════

def merge(state: JourneyState) -> dict:
    """Barrier. Waits for all 4 parallel front nodes."""
    return {}

def route_from_merge(state: JourneyState) -> str:
    """Edge labels naming the transition. Safety-first; then intent."""
    if state.get("is_emergency"):
        return "emergency_short_circuit"
    if state.get("ip_locked"):
        return "iplocked_short_circuit"
    if state.get("is_repetitive"):
        return "repetitive_short_circuit"
    intent = state.get("intent", "care_giver")
    return {
        "exit":              "user_exit_intent",
        "clarify":           "clarify_request",
        "clinical_advice":   "clinical_advice_short_path",
        "speak_to_us":       "lead_capture_intent",
        "unknown":           "unknown_question_intent",
        "about_skip":        "about_skip_intent",
        "about_chathealthy": "about_ch_intent",
        "care_giver":        "care_giver_intent",
        "clinical_trial":    "clinical_trial_intent",
    }.get(intent, "care_giver_intent")


# ══════════════════════════════════════════════════════════════
# Tools — one per real LLM-invokable business operation
# ══════════════════════════════════════════════════════════════

def choose_specialties(state: JourneyState) -> dict:
    """LLM-invoked. Internally runs vector search + homeopathic expansion
    (sub-routines). Sets utterance_type='specialties'."""
    return {"specialties": [], "homeopathic_expansion": [],
            "utterance_type": "specialties"}

def tool_find_providers(state: JourneyState) -> dict:
    """DB query for providers matching the user's chosen specialty codes."""
    return {"providers": [], "utterance_type": "providers"}

def tool_search_clinical_trials(state: JourneyState) -> dict:
    """clinicaltrials.gov API."""
    return {"trials": [], "utterance_type": "trials"}

def tool_record_user_details(state: JourneyState) -> dict:
    """Lead capture."""
    return {"utterance_type": "lead_captured"}

def tool_record_unknown_question(state: JourneyState) -> dict:
    """Log unanswerable question for product learning."""
    return {"utterance_type": "unknown_logged"}

def tool_get_skip_snow_context(state: JourneyState) -> dict:
    """Return context paragraphs about Skip."""
    return {"utterance_type": "about_skip"}

def tool_get_chathealthy_context(state: JourneyState) -> dict:
    """Return about-ChatHealthy paragraphs."""
    return {"utterance_type": "about_chathealthy"}


# ══════════════════════════════════════════════════════════════
# System utterance (the ONE place the system speaks)
# ══════════════════════════════════════════════════════════════

def system_utterance(state: JourneyState) -> dict:
    """SYSTEM ACTOR. Composes the user-facing message based on
    state.utterance_type. LLM call or template, depending on type."""
    utype = state.get("utterance_type", "general")
    return {"response": {"type": utype, "message": f"<stub utterance: {utype}>"}}


# ══════════════════════════════════════════════════════════════
# Terminal
# ══════════════════════════════════════════════════════════════

def evaluate_handoff(state: JourneyState) -> dict:
    """Terminal. Hand selected items to EvaluateCare. Mission success."""
    return {"response": {"type": "handoff", "target": "EvaluateCare"}}

def user_exits(state: JourneyState) -> dict:
    """Terminal. User said goodbye / done. Session ends gracefully."""
    return {"response": {"type": "exit", "message": "<stub: goodbye>"}}


# ══════════════════════════════════════════════════════════════
# Graph build
# ══════════════════════════════════════════════════════════════

def build_user_journey():
    g = StateGraph(JourneyState)

    # Loop top: user_utterance → ip_lock_check (sequential, lockout trumps safety)
    g.add_node("user_utterance", user_utterance)
    g.add_node("ip_lock_check", ip_lock_check)
    g.add_node("get_authorization", get_authorization)

    # Per-utterance parallel safety + classifier (only entered when unlocked + authed)
    g.add_node("emergency_detector", emergency_detector)
    g.add_node("repetitive_detector", repetitive_detector)
    g.add_node("classifier", classifier)
    g.add_node("merge", merge)

    # Tools
    g.add_node("choose_specialties", choose_specialties)
    g.add_node("tool_search_clinical_trials", tool_search_clinical_trials)
    g.add_node("tool_record_user_details", tool_record_user_details)
    g.add_node("tool_record_unknown_question", tool_record_unknown_question)
    g.add_node("tool_get_skip_snow_context", tool_get_skip_snow_context)
    g.add_node("tool_get_chathealthy_context", tool_get_chathealthy_context)

    # System utterance + terminals
    g.add_node("system_utterance", system_utterance)
    g.add_node("evaluate_handoff", evaluate_handoff)
    g.add_node("user_exits", user_exits)

    # ip_lock_check is FIRST — precedes utterance, auth, everything.
    g.add_edge(START, "ip_lock_check")

    # ip_lock_check routes (4 possible next steps):
    #   locked → system_utterance
    #   unlocked + no auth → get_authorization
    #   unlocked + authed + has-message → fan out to safety + classifier
    #   unlocked + authed + no-message → user_utterance (straight)
    g.add_conditional_edges("ip_lock_check", lock_route, [
        "user_exits", "user_utterance", "get_authorization",
    ])

    # user_utterance loops back to ip_lock_check (lockout always re-checks)
    g.add_edge("user_utterance", "ip_lock_check")

    # Parallel safety + classifier → merge (barrier)
    g.add_edge("emergency_detector", "merge")
    g.add_edge("repetitive_detector", "merge")
    g.add_edge("classifier", "merge")

    # ── Merge routes to ONE next step ──
    g.add_conditional_edges("merge", route_from_merge, {
        "emergency_short_circuit":     "system_utterance",
        "iplocked_short_circuit":      "system_utterance",
        "repetitive_short_circuit":    "system_utterance",
        "user_exit_intent":            "user_exits",        # 2nd terminal
        "clarify_request":             "system_utterance",
        "clinical_advice_short_path":  "system_utterance",
        "lead_capture_intent":         "tool_record_user_details",
        "unknown_question_intent":     "tool_record_unknown_question",
        "about_skip_intent":           "tool_get_skip_snow_context",
        "about_ch_intent":             "tool_get_chathealthy_context",
        "care_giver_intent":           "choose_specialties",
        "clinical_trial_intent":       "tool_search_clinical_trials",
    })

    # get_authorization → user_utterance only (the 3 parallel are now
    # triggered by user_utterance — without an utterance there's nothing
    # to evaluate).
    g.add_edge("get_authorization", "user_utterance")

    # user_utterance fans out to the 3 parallel safety + classifier.
    g.add_edge("user_utterance", "emergency_detector")
    g.add_edge("user_utterance", "repetitive_detector")
    g.add_edge("user_utterance", "classifier")

    # choose_specialties → tool_find_providers
    # tool_find_providers leads to evaluate_handoff (find provider leads
    # to evaluate care) AND to system_utterance (so the provider list
    # gets spoken to the user)
    g.add_node("tool_find_providers", tool_find_providers)
    g.add_edge("choose_specialties", "tool_find_providers")
    g.add_edge("user_utterance", "tool_find_providers")
    g.add_edge("tool_find_providers", "system_utterance")
    g.add_edge("tool_find_providers", "evaluate_handoff")

    # tool_search_clinical_trials → evaluate_handoff (alpha-mission path)
    # AND → system_utterance (so user sees the trial list).
    g.add_edge("tool_search_clinical_trials", "system_utterance")
    g.add_edge("tool_search_clinical_trials", "evaluate_handoff")

    # Other tools → system_utterance directly
    for name in ("tool_record_user_details",
                 "tool_record_unknown_question",
                 "tool_get_skip_snow_context",
                 "tool_get_chathealthy_context"):
        g.add_edge(name, "system_utterance")

    # ── system_utterance always loops back to user_utterance ──
    g.add_edge("system_utterance", "user_utterance")

    # ── Terminals (two) ──
    g.add_edge("evaluate_handoff", END)
    g.add_edge("user_exits", END)

    return g.compile()


user_journey = build_user_journey()
