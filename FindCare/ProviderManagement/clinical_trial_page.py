# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""The clinical-trial page handler, drained out of app.py.

app.py's /trial/find route collects the request, proves the gateway
signature, and hands the turn here. This owns the clinical-trial page: mine
the condition and what narrows it, write it to the page before the search,
read back what is in force, and stream the criteria then the trials as they
arrive (design V2 §2.5).
"""
from __future__ import annotations

import asyncio
import json as _json

from fastapi.responses import StreamingResponse

from db_config import CLINICAL_TRIAL_PAGE
from page_parameters import (parameter_entry, parameters_in_force,
                             write_page_parameters)
from ProviderManagement.clinical_trial_utterance import (
    mine_clinical_trial_parameters)

try:
    from ClinicalTrials import clinical_trials_tool
except ImportError:
    from FindCare.ClinicalTrials import clinical_trials_tool


def _trial_page_entries(mined) -> dict:
    """The mined values as parameter entries, keyed by attribute."""
    entries: dict = {}
    if mined.condition:
        entries["condition"] = parameter_entry(mined.condition)
    if mined.age_years is not None:
        entries["ageYears"] = parameter_entry(int(mined.age_years))
    if mined.sex:
        entries["sex"] = parameter_entry(mined.sex)
    if mined.united_states_only is not None:
        entries["unitedStatesOnly"] = parameter_entry(
            bool(mined.united_states_only))
    return entries


async def find(utterance: str, history: list) -> StreamingResponse:
    """The clinical-trial page mines its own parameters and searches on
    them, streaming the trials as they arrive.

    The criteria are announced on the same stream rather than by the caller:
    the caller posted an utterance and has not read it, so it has nothing to
    announce until this page says what the utterance meant.
    """
    # Read what the page holds BEFORE this turn's mining overwrites it, so a
    # continuation -- the same condition, or "get me more" naming none -- can
    # carry forward the cursor the last search left, while a NEW condition
    # starts the list fresh.
    prior = await asyncio.to_thread(parameters_in_force, CLINICAL_TRIAL_PAGE)
    # The mining awaits a model call on this loop.
    mined = await mine_clinical_trial_parameters(utterance, history)
    prior_condition = str(prior.get("condition") or "").strip().lower()
    mined_condition = (mined.condition or "").strip().lower()
    is_continuation = bool(prior_condition) and (
        not mined_condition or mined_condition == prior_condition)
    extend_cursor = str(prior.get("cursor") or "") if is_continuation else ""
    # A new search must not inherit the prior list's cursor or count.
    if not is_continuation:
        await asyncio.to_thread(
            write_page_parameters, CLINICAL_TRIAL_PAGE,
            {"cursor": parameter_entry(""), "resultCount": parameter_entry(0)})
    await asyncio.to_thread(
        write_page_parameters, CLINICAL_TRIAL_PAGE, _trial_page_entries(mined))

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    class _StreamCollector:
        def stream(self, event):
            queue.put_nowait(event)

    deps = _StreamCollector()
    # What the search runs on is what is in force. The mined values were
    # written above, so the session is the merge of everything said so far --
    # a person who names a condition on one turn and their age on the next
    # has given both, and searching on the latest turn alone would throw the
    # condition away.
    in_force = await asyncio.to_thread(parameters_in_force, CLINICAL_TRIAL_PAGE)
    age = in_force.get("ageYears")
    scope = "us" if in_force.get("unitedStatesOnly") else "international"
    req = clinical_trials_tool.Request(
        condition=str(in_force.get("condition") or ""),
        age_years=int(age) if age is not None else None,
        sex=str(in_force.get("sex") or "") or None,
        geographic_scope=scope,
        cursor=extend_cursor or None,
    )
    # What the person asked for, said back to them in words. Composed here
    # because it is a sentence about this page's criteria.
    said_back = []
    if in_force.get("condition"):
        said_back.append(f"condition: {in_force['condition']}")
    if age is not None:
        said_back.append(f"subject age: {age}")
    if in_force.get("sex"):
        said_back.append(f"subject sex: {in_force['sex']}")
    said_back.append("scope: US" if in_force.get("unitedStatesOnly")
                     else "scope: international")
    announced = {
        "kind": "intent_classified",
        "data": {
            "action": "findClinicalTrials",
            "condition": in_force.get("condition"),
            "age_years": age,
            "sex": str(in_force.get("sex") or "") or None,
            "geographic_scope": scope,
            "criteria_summary": ", ".join(said_back),
        },
    }

    async def runner():
        try:
            resp = await clinical_trials_tool.TOOL.run(deps, req)
            # Get the cursor back onto the page. The tool answers with the
            # position the next registry batch continues from and the count
            # it established; both are recorded on the page the way the
            # other pages record their position, so a later turn can extend
            # the list past what is shown instead of starting over, and the
            # "of N" the person is shown has a number behind it.
            if resp is not None:
                await asyncio.to_thread(
                    write_page_parameters, CLINICAL_TRIAL_PAGE, {
                        "cursor": parameter_entry(resp.cursor or ""),
                        "resultCount": parameter_entry(
                            int(resp.total_count or 0)),
                    })
        finally:
            queue.put_nowait(sentinel)

    asyncio.create_task(runner())

    async def gen():
        yield _json.dumps(announced).encode() + b"\n"
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield _json.dumps(item).encode() + b"\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")
