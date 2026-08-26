# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""End-to-end requirement tests for the UM/UR partial-information dispatch
contract introduced under EPIC-002-F-010-S-001 (UM), EPIC-002-F-010-S-002
(FindCare-UM), EPIC-002-F-004-S-001 (UR), and EPIC-002-F-004-S-002
(FindCare-UR). One test per requirement; all run with deterministic
in-process mocks rather than the live LLM."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

import pytest

from chathealthy_lib.authentication.agent_deps import AgentDeps
from chathealthy_lib.authentication.user_object import (
    Action,
    SessionConversationHistory,
    UserObject,
    Utterance,
)

from UtteranceManager import utterance_manager as um
from chathealthy_lib.authentication.intent_document import (
    Argument,
    IntentDocument,
    IntentFindAProvider,
    IntentSpecialtySearch,
    IntentCloseConnection200,
    PendingDisambiguation,
)


# ────────────────────────────────────────────────────────────────────
# Fixtures
# ────────────────────────────────────────────────────────────────────


_STAMP = "2026-06-09T17:00:00 PST"


def _person(n: int, text: str) -> Utterance:
    return Utterance(n=n, at=_STAMP, actor="person", text=text)


def _system(n: int, text: str) -> Utterance:
    return Utterance(n=n, at=_STAMP, actor="system", text=text)


def _make_user_object(utterances=None, actions=None, intent=None) -> UserObject:
    """Build a minimal UserObject with a session_conversation_history and
    optional prior IntentDocument."""
    history = SessionConversationHistory(
        utterances=list(utterances or []),
        actions=list(actions or []),
    )
    uo = UserObject(
        current_session_token="NULL",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        session_conversation_history=history,
        intent=intent,
    )
    return uo


def _make_deps(user_object: UserObject) -> tuple[AgentDeps, list[dict]]:
    """Build an AgentDeps with a stream sink that captures events into a
    list. Returns (deps, events) so tests can inspect emitted events."""
    events: list[dict] = []
    deps = AgentDeps(
        user_object=user_object,
        session_token=user_object.current_session_token,  # "NULL" sentinel
        mongo_frontend=None,
        server_env="local",
        stream=events.append,
    )
    return deps, events


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-010-S-001-REQ-B-006 — labeled transcript window
# ────────────────────────────────────────────────────────────────────


def test_req_um_b006_transcript_window_labels_actor_and_caps_at_10():
    """The transcript window returns up to 10 dialogue lines, oldest
    first, each labeled 'user:' or 'system:'. Both actors are included so
    a follow-up turn ('yes') can resolve against the prior system line."""
    utterances = []
    n = 1
    for i in range(8):
        utterances.append(_person(n, f"user-{i}")); n += 1
        utterances.append(_system(n, f"system-{i}")); n += 1
    uo = _make_user_object(utterances=utterances)
    deps, _ = _make_deps(uo)
    window = um._recent_transcript(deps)
    assert len(window) == 10
    # Cap = 10; oldest-first; both actors interleaved.
    assert window[0].startswith("user:") or window[0].startswith("system:")
    assert "user: user-7" in window or "system: system-7" in window
    # Last line in the window should be the most recent system line.
    assert window[-1] == "system: system-7"


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-010-S-001-REQ-B-007 — ambiguous-with-candidate state
# ────────────────────────────────────────────────────────────────────


def test_req_um_b007_classifier_output_accepts_pending_disambiguation():
    out = um._ClassifierOutput(
        target_action="specialtySearch",
        complaint="back pain",
        geography=um._GeoFacts(city="milwaukee"),
        user_message="Did you mean Milwaukee, Wisconsin?",
        pending_disambiguation=um._PendingDisambiguationOut(
            kind="geography_state", candidate={"state": "WI"},
        ),
    )
    pending = um._to_pending(out.pending_disambiguation)
    assert isinstance(pending, PendingDisambiguation)
    assert pending.kind == "geography_state"
    assert pending.candidate == {"state": "WI"}


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-010-S-001-REQ-B-008 — yes/no resolution on the next turn
# ────────────────────────────────────────────────────────────────────


def test_req_um_b008_yes_resolution_upgrades_to_find_a_provider():
    prior = IntentDocument(
        target_action="specialtySearch",
        intents=[
            IntentSpecialtySearch(
                name="specialtySearch",
                arguments=[Argument(name="complaint", value="back pain", type="string", required=True)],
            ),
            IntentFindAProvider(
                name="findAProvider",
                arguments=[
                    Argument(name="complaint", value="back pain", type="string", required=True),
                    Argument(name="geography", value=json.dumps({"city": "milwaukee"}), type="object", required=True),
                ],
                pending_disambiguation=PendingDisambiguation(
                    kind="geography_state", candidate={"state": "WI"},
                ),
            ),
        ],
        user_message="Did you mean Milwaukee, Wisconsin?",
    )
    utterances = [
        _person(1, "I need a doctor for back pain in milwaukee"),
        _system(2, "Did you mean Milwaukee, Wisconsin?"),
        _person(3, "yes"),
    ]
    uo = _make_user_object(utterances=utterances, intent=prior)
    deps, events = _make_deps(uo)

    llm_result = um._ClassifierOutput(
        target_action="findAProvider",
        complaint="back pain",
        geography=um._GeoFacts(state="WI", city="milwaukee"),
        user_message=None,
        pending_disambiguation=None,
    )
    with patch.object(um, "_call_classifier_llm", new=AsyncMock(return_value=llm_result)):
        asyncio.run(um.TOOL.run(deps, um.Request()))

    new_doc = uo.intent
    assert new_doc is not None
    assert new_doc.target_action == "findAProvider"
    find_entry = next(i for i in new_doc.intents if i.name == "findAProvider")
    assert find_entry.pending_disambiguation is None
    geo_arg = next(a for a in find_entry.arguments if a.name == "geography")
    assert json.loads(geo_arg.value).get("state") == "WI"


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-010-S-001-REQ-B-009 — user_message: streamed AND persisted
# ────────────────────────────────────────────────────────────────────


def test_req_um_b009_user_message_streamed_and_persisted_as_system_utterance():
    """When the classifier returns user_message, UM (a) emits a
    kind:'prompt' wire event and (b) appends a SYSTEM utterance to the
    session bucket so the next UM call sees it on the transcript."""
    uo = _make_user_object(utterances=[_person(1, "hello")])
    deps, events = _make_deps(uo)

    llm_result = um._ClassifierOutput(
        target_action="closeConnection200",
        user_message="Hi! What can I help you find?",
    )
    with patch.object(um, "_call_classifier_llm", new=AsyncMock(return_value=llm_result)):
        asyncio.run(um.TOOL.run(deps, um.Request()))

    assert any(
        e.get("kind") == "prompt"
        and e.get("data", {}).get("text") == "Hi! What can I help you find?"
        for e in events
    ), f"missing kind:'prompt' wire event: {events}"
    final_utts = uo.session_conversation_history.utterances
    sys_lines = [u for u in final_utts if u.actor == "system"]
    assert sys_lines, "no system utterance persisted"
    assert sys_lines[-1].text == "Hi! What can I help you find?"


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-010-S-001-REQ-B-010 — no hardcoded clarification strings
# ────────────────────────────────────────────────────────────────────


def test_req_um_b010_no_hardcoded_clarification_in_tools():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[3]
    targets = [
        root / "sharedServices/Code/CloseConnection200Tool/close_connection_200_tool.py",
    ]
    bad = []
    forbidden = (
        "I didn't catch that",
        "could you rephrase",
        "Sorry, I",
        "could you tell me",
    )
    for path in targets:
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                bad.append((str(path), needle))
    assert not bad, f"hardcoded chat strings found: {bad}"


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-004-S-001-REQ-B-002 + B-003 — cache hit skips SpecialtyFilter
# ────────────────────────────────────────────────────────────────────


def test_req_ur_b002_b003_cache_hit_skips_specialty_filter():
    from authentication import universal_navigation_tool as nav

    cached_codes = [{"code": "207X00000X", "name": "Orthopaedic Surgery Physician", "score": 0.9}]
    intent_doc = IntentDocument(
        target_action="specialtySearch",
        intents=[
            IntentSpecialtySearch(
                name="specialtySearch",
                arguments=[
                    Argument(name="complaint", value="back pain", type="string", required=True),
                    Argument(name="nucc_codes", value=json.dumps(cached_codes), type="array", required=False),
                ],
            ),
        ],
    )
    uo = _make_user_object(utterances=[_person(1, "ignored")], intent=intent_doc)
    deps, events = _make_deps(uo)

    from SpecialtyFilter import specialty_filter_tool as sft
    with patch.object(sft.TOOL, "run_and_log", new=AsyncMock(side_effect=AssertionError("should not run"))):
        codes = asyncio.run(nav.TOOL._run_or_cache_specialty_filter(deps, "back pain"))

    assert codes == cached_codes
    assert any(e.get("kind") == "specialties" for e in events), events


def test_req_ur_b003_first_run_writes_nucc_codes_to_intent():
    from authentication import universal_navigation_tool as nav

    intent_doc = IntentDocument(
        target_action="specialtySearch",
        intents=[
            IntentSpecialtySearch(
                name="specialtySearch",
                arguments=[Argument(name="complaint", value="back pain", type="string", required=True)],
            ),
        ],
    )
    uo = _make_user_object(utterances=[_person(1, "ignored")], intent=intent_doc)
    deps, _ = _make_deps(uo)

    from SpecialtyFilter import specialty_filter_tool as sft

    class FakeRow:
        def __init__(self, code, name, score):
            self.code, self.name, self.score = code, name, score
        def model_dump(self, exclude_none=False):
            return {"code": self.code, "name": self.name, "score": self.score}

    class FakeResponse:
        error = None
        specialties = [FakeRow("207X00000X", "Orthopaedic Surgery Physician", 0.9)]
        # The specialty step returns the clinical reading of the utterance
        # alongside the codes, and UR writes it to userParameters.complaint.
        complaint = "back problem"

    with patch.object(sft.TOOL, "run_and_log", new=AsyncMock(return_value=FakeResponse())):
        codes = asyncio.run(nav.TOOL._run_or_cache_specialty_filter(deps, "back pain"))

    assert codes and codes[0]["code"] == "207X00000X"
    new_doc = uo.intent
    spec_entry = next(i for i in new_doc.intents if i.name == "specialtySearch")
    nucc_arg = next(a for a in spec_entry.arguments if a.name == "nucc_codes")
    parsed = json.loads(nucc_arg.value)
    assert parsed[0]["code"] == "207X00000X"


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-004-S-001-REQ-B-004 — pending suppresses closeConnection200
# ────────────────────────────────────────────────────────────────────


def test_req_ur_b004_pending_disambiguation_detected_on_document():
    from authentication import universal_navigation_tool as nav

    doc_with_pending = IntentDocument(
        target_action="closeConnection200",
        intents=[
            IntentCloseConnection200(
                name="closeConnection200",
                arguments=[Argument(name="close_connection", value="true", type="boolean", required=True)],
            ),
            IntentFindAProvider(
                name="findAProvider",
                arguments=[
                    Argument(name="complaint", value="x", type="string", required=True),
                ],
                pending_disambiguation=PendingDisambiguation(
                    kind="geography_state", candidate={"state": "WI"},
                ),
            ),
        ],
    )
    doc_without_pending = IntentDocument(
        target_action="closeConnection200",
        intents=[
            IntentCloseConnection200(
                name="closeConnection200",
                arguments=[Argument(name="close_connection", value="true", type="boolean", required=True)],
            ),
        ],
    )
    assert nav._any_pending_disambiguation(doc_with_pending) is True
    assert nav._any_pending_disambiguation(doc_without_pending) is False


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-010-S-002-REQ-B-001 — FindCare-UM geography slot
# ────────────────────────────────────────────────────────────────────


def test_req_findcare_um_b001_ambiguous_city_parks_findap_with_pending():
    uo = _make_user_object(utterances=[_person(1, "back pain in milwaukee")])
    deps, _ = _make_deps(uo)

    llm_result = um._ClassifierOutput(
        target_action="specialtySearch",
        complaint="back pain",
        geography=um._GeoFacts(city="milwaukee"),
        user_message="Did you mean Milwaukee, Wisconsin?",
        pending_disambiguation=um._PendingDisambiguationOut(
            kind="geography_state", candidate={"state": "WI"},
        ),
    )
    with patch.object(um, "_call_classifier_llm", new=AsyncMock(return_value=llm_result)):
        asyncio.run(um.TOOL.run(deps, um.Request()))

    doc = uo.intent
    assert doc.target_action == "specialtySearch"
    find_entry = next((i for i in doc.intents if i.name == "findAProvider"), None)
    assert find_entry is not None, "expected a parked IntentFindAProvider entry"
    assert find_entry.pending_disambiguation is not None
    assert find_entry.pending_disambiguation.kind == "geography_state"
    geo_arg = next(a for a in find_entry.arguments if a.name == "geography")
    parsed_geo = json.loads(geo_arg.value)
    assert parsed_geo.get("city") == "milwaukee"
    assert "state" not in parsed_geo or not parsed_geo.get("state")


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-004-S-002-REQ-B-001 — FindCare-UR nucc_codes caching
# ────────────────────────────────────────────────────────────────────


def test_req_findcare_ur_b001_nucc_codes_cached_on_findcare_intents():
    from authentication import universal_navigation_tool as nav

    intent_doc = IntentDocument(
        target_action="specialtySearch",
        intents=[
            IntentSpecialtySearch(
                name="specialtySearch",
                arguments=[Argument(name="complaint", value="back pain", type="string", required=True)],
            ),
            IntentFindAProvider(
                name="findAProvider",
                arguments=[
                    Argument(name="complaint", value="back pain", type="string", required=True),
                    Argument(name="geography", value=json.dumps({"city": "milwaukee"}), type="object", required=True),
                ],
                pending_disambiguation=PendingDisambiguation(
                    kind="geography_state", candidate={"state": "WI"},
                ),
            ),
        ],
    )
    uo = _make_user_object(utterances=[_person(1, "ignored")], intent=intent_doc)
    deps, _ = _make_deps(uo)

    from SpecialtyFilter import specialty_filter_tool as sft

    class FakeRow:
        def __init__(self, code, name, score):
            self.code, self.name, self.score = code, name, score
        def model_dump(self, exclude_none=False):
            return {"code": self.code, "name": self.name, "score": self.score}

    class FakeResponse:
        error = None
        specialties = [FakeRow("207X00000X", "Orthopaedic Surgery Physician", 0.9)]
        # The specialty step returns the clinical reading of the utterance
        # alongside the codes, and UR writes it to userParameters.complaint.
        complaint = "back problem"

    with patch.object(sft.TOOL, "run_and_log", new=AsyncMock(return_value=FakeResponse())):
        asyncio.run(nav.TOOL._run_or_cache_specialty_filter(deps, "back pain"))

    new_doc = uo.intent
    for nm in ("specialtySearch", "findAProvider"):
        entry = next(i for i in new_doc.intents if i.name == nm)
        nucc = next((a for a in entry.arguments if a.name == "nucc_codes"), None)
        assert nucc is not None, f"{nm} missing nucc_codes after cache write"
        assert json.loads(nucc.value)[0]["code"] == "207X00000X"


# ────────────────────────────────────────────────────────────────────
# EPIC-002-F-004-S-002-REQ-B-002 — ProviderSearch sufficiency gate
# ────────────────────────────────────────────────────────────────────


def test_req_findcare_ur_b002_validate_rejects_partial_geography():
    from authentication import universal_navigation_tool as nav

    bad_doc = IntentDocument(
        target_action="findAProvider",
        intents=[
            IntentFindAProvider(
                name="findAProvider",
                arguments=[
                    Argument(name="complaint", value="back pain", type="string", required=True),
                    Argument(name="geography", value=json.dumps({"city": "milwaukee"}), type="object", required=True),
                ],
            ),
        ],
    )
    with pytest.raises(RuntimeError, match="geography insufficient"):
        nav.TOOL._validate_document(bad_doc, "findAProvider")
