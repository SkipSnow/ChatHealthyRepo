# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# find_specialists — the FindCare specialty filter tool.
#
# V5 architecture (LLM-picks):
#   Single Pydantic-AI agent (Gemini Flash Lite preview) walks the FULL
#   enriched NUCC corpus (Section=Individual minus the 38 static admin /
#   non-clinical codes minus Deactivated entries) in one LLM call and
#   directly picks the codes that match the user's clinical intent.
#   The same call emits natural-language `include` / `exclude`
#   paragraphs (audit fields) and a list of `nucc_codes` with LLM-judged
#   relevance scores in [0,1]. No embedding step. No Atlas $vectorSearch.
#   No normalize-then-filter pipeline.
from __future__ import annotations

import json
import os
import threading
from typing import Any

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pymongo.collection import Collection

from chathealthy_frontend_lib import run_llm_sync
from chathealthy_frontend_lib.exceptions import ChatHealthyException


# ── Pydantic I/O contracts ────────────────────────────────────────────
class NuccCodePick(BaseModel):
    """One NUCC code picked by the LLM."""
    code: str = Field(..., min_length=10, max_length=10,
                      description="NUCC 10-character alphanumeric code")
    score: float = Field(..., ge=0.0, le=1.0,
                         description="LLM-judged relevance likelihood [0.0, 1.0]")


class AllPossibleRecord(BaseModel):
    """One row in the enriched 'all possible' union the tool delivers to the
    SpecialtyFilter CSS. `homeopathic_general=False` is a list-one member
    (the LLM picked it for THIS query); `homeopathic_general=True` is a
    list-two member (the static homeopathic-generalist fallback set,
    same for every query). Both lists are always present in the union.
    """
    code: str = Field(..., min_length=10, max_length=10)
    name: str = Field(..., description="Display Name from SpecialtyMetaData")
    can_prescribe: bool
    homeopathic: bool
    homeopathic_general: bool
    rank: int = Field(..., description="Ordering hint; list-one first then list-two")


class FindSpecialistsOutput(BaseModel):
    """Tool output.

    `include` and `exclude` are natural-language paragraphs the LLM emits
    alongside its picks — they describe, in clinical terms, what kinds of
    providers the LLM judged to be in-scope vs. out-of-scope for the
    user's request. They are audit fields, not retrieval inputs (V5 does
    not embed them; the single LLM call already produced the picks).

    `nucc_codes` is the flat list of picked codes with their LLM-judged
    scores in [0,1], ordered highest first. This is the V5 picks only —
    it does NOT include the homeopathic-generalist fallback set.

    `all_possible` is the union the SpecialtyFilter CSS renders directly:
    list-one (LLM picks, homeopathic_general=False) followed by list-two
    (static homeopathic generalists, homeopathic_general=True), each
    enriched with Display Name + can_prescribe + homeopathic flags from
    SpecialtyMetaData. The tool owns this assembly per EPIC-006-F-002 —
    the CSS gets everything it needs to deterministically render from a
    single tool result.
    """
    include: str = Field(..., min_length=50,
                         description="Natural-language paragraph describing the in-scope clinical intent (≥50 chars)")
    exclude: str = Field(..., min_length=50,
                         description="Natural-language paragraph describing what is out-of-scope (≥50 chars; substantive contrastive signal)")
    nucc_codes: list[NuccCodePick] = Field(..., min_length=0)
    all_possible: list[AllPossibleRecord] = Field(default_factory=list,
                                                  description="Enriched union of LLM picks + static homeopathic generalists")


# ── Static admin/non-clinical NUCC code exclusion (day-1) ─────────────
EXCLUDED_CODES: list[str] = [
    "171W00000X", "171WH0202X", "171WV0202X", "172A00000X", "176P00000X",
    "1744G0900X", "174H00000X", "171400000X", "171R00000X", "173000000X",
    "405300000X", "1744R1103X", "1744R1102X", "174400000X", "1744P3200X",
    "174M00000X", "174MM1900X", "246Y00000X", "246YC3301X", "246YC3302X",
    "246YR1600X", "246ZB0500X", "246ZB0301X", "246ZB0302X", "246ZB0600X",
    "246ZG0701X", "246ZA2600X", "246ZI1000X", "246QL0901X", "246QL0900X",
    "2470A2800X", "247000000X", "2472B0301X", "2472D0500X", "2472V0600X",
    "246Z00000X", "247200000X", "390200000X",
]
_EXCLUDED_SET: frozenset[str] = frozenset(EXCLUDED_CODES)


# ── Model wiring ──────────────────────────────────────────────────────
PICK_MODEL_ID = "google:gemini-3.1-flash-lite-preview"


# ── Prompt ────────────────────────────────────────────────────────────
_PICK_SYSTEM_PROMPT = """
You are the FindCare specialty filter. In ONE step, given a vernacular
user healthcare request and the FULL enriched NUCC provider-taxonomy
corpus (filtered to individual human practitioners, with day-1 admin
codes and Deactivated entries already removed), you must:

  1. Decide which NUCC codes match the user's clinical intent.
  2. Emit a natural-language `include` paragraph describing the kinds
     of providers that ARE in scope.
  3. Emit a natural-language `exclude` paragraph describing the kinds
     of providers that are OUT of scope (the contrastive signal).
  4. Emit a flat `nucc_codes` list of the codes you picked, each with a
     LLM-judged relevance score in [0.0, 1.0].

We are optimizing for EXCELLENT RECALL AND PRECISION. We only care about
INDIVIDUAL HUMAN PRACTITIONERS in the result set; institutions, agencies,
facilities, clinics, suppliers, and other non-individual NUCC entries
are already filtered out of the corpus you receive.

CORPUS RECORD SHAPE — each record you receive has:
  Code, Display Name, Grouping, Classification, Specialization, Definition

IMPORTANT — `Grouping` and `Classification` are HINTS, not
determinative. The user's clinical intent can be served by records whose
grouping has nothing to do with the words the user used. For example,
"Pediatric Surgery Physician" sits under `Classification: Surgery`, NOT
`Pediatrics`. Anchor on intent.

INFER GENDER AND AGE WHEN POSSIBLE:
If the user's wording lets you infer the patient's gender or age
(e.g. "my husband's prostate" → adult male; "kid's doc" → pediatric
population; "for my mom, she's 78" → elderly female), factor those
into your picks and your include/exclude reasoning. If gender or age
is NOT inferable, do NOT invent either — keep variants in.

CAM / HOLISTIC SAFEGUARD:
Do NOT push away from complementary, alternative, integrative, or
holistic medicine providers (acupuncturists, naturopaths, chiropractors,
naprapaths, homeopaths, mechanotherapists, massage therapists, midwives,
etc.) just because they are CAM. They are licensed in many U.S.
jurisdictions and may be the right answer. Drop them only if they are
clinically off-target for the user's specific request.

USER-NAMED PROVIDER TYPE IS BINDING:
If the user explicitly names a provider type, role, license, or
credential (e.g. "nurse practitioner", "psychiatrist", "MD", "NP",
"PA", "DO", "midwife", "shrink", "bone doctor", "chiropractor"),
EVERY NUCC code matching that named type MUST appear in `nucc_codes`
with score 1.0. You MAY also include adjacent provider types that fit
the clinical intent, but they MUST score strictly lower (≤ 0.7). The
named type leads the list. Omitting the named type is a hard error.

SCORING — `score` is YOUR judgment of relevance for each picked code:
  1.0 = exactly what they asked for
  0.7 = strong adjacent option
  0.4 = borderline kept for recall
When in doubt, KEEP a candidate with a lower score. Missing a true
match is worse than including a borderline one. Use ONLY codes that
appear in the supplied corpus — do not invent codes.

OUTPUT — strict, typed JSON object with these fields:
  - `include`: string, ≥50 characters, substantive natural-language
    paragraph describing the in-scope clinical intent.
  - `exclude`: string, ≥50 characters, substantive contrastive
    natural-language paragraph describing what is out-of-scope.
  - `nucc_codes`: list of objects, each `{ "code": <NUCC 10-char>,
    "score": <float 0.0–1.0> }`. Order by score, highest first.

Both `include` and `exclude` MUST be substantive (≥50 chars). A short
or empty `exclude` collapses the contrastive signal and is rejected.
No preamble, no markdown fences, no explanation outside the JSON.
""".strip()


_PICK_USER_TEMPLATE = """
USER QUERY:
{user_query}

NUCC CORPUS ({n_records} records — Section=Individual, minus 38 admin
codes, minus Deactivated). One JSON object per record:
{corpus_block}

Emit ONLY the JSON object described in the system prompt.
""".strip()


_thread_local = threading.local()


def _get_pick_agent() -> Agent:
    # Thread-local Agent cache. pydantic-ai's Agent.run_sync wraps
    # asyncio.run, which creates a new event loop per call. The Agent's
    # internal asyncio.Event becomes bound to the first call's loop;
    # subsequent calls from a different worker thread bind to a different
    # loop and raise "Event is bound to a different event loop".
    # asyncio.to_thread uses a bounded ThreadPoolExecutor (≈cpu_count+4),
    # so each thread caches its own Agent constructed-once-then-reused.
    cached = getattr(_thread_local, "agent", None)
    if cached is not None:
        return cached
    if not os.getenv("GEMINI_API_KEY"):
        raise ChatHealthyException(
            mode="gemini_api_key_missing",
            message="GEMINI_API_KEY not set; required for find_specialists.",
            component="SpecialtyFilter",
        )
    _thread_local.agent = Agent(
        PICK_MODEL_ID,
        output_type=FindSpecialistsOutput,
        system_prompt=_PICK_SYSTEM_PROMPT,
        model_settings={"temperature": 0.3},
        retries=5,
    )
    return _thread_local.agent


# ── Corpus load ───────────────────────────────────────────────────────
def _load_corpus(col: Collection) -> list[dict[str, Any]]:
    """Load the enriched NUCC corpus the LLM will walk.

    Filter: Section=Individual, Code not in EXCLUDED_CODES, and any
    record whose Display Name starts with 'Deactivated' is dropped.
    """
    projection = {
        "_id": 0,
        "Code": 1,
        "Display Name": 1,
        "Grouping": 1,
        "Classification": 1,
        "Specialization": 1,
        "Definition": 1,
    }
    cursor = col.find(
        {
            "Section": "Individual",
            "Code": {"$nin": EXCLUDED_CODES},
        },
        projection,
        batch_size=1000,
    )
    out: list[dict[str, Any]] = []
    for d in cursor:
        if (d.get("Display Name") or "").startswith("Deactivated"):
            continue
        if d.get("Code") in _EXCLUDED_SET:
            # belt-and-suspenders in case server-side $nin missed
            continue
        out.append({
            "Code": d.get("Code", ""),
            "Display Name": d.get("Display Name", ""),
            "Grouping": d.get("Grouping", ""),
            "Classification": d.get("Classification", ""),
            "Specialization": d.get("Specialization", ""),
            "Definition": (d.get("Definition") or "").strip(),
        })
    return out


def _format_corpus(corpus: list[dict[str, Any]]) -> str:
    """One compact JSON object per line — cheap on tokens, easy for the
    LLM to walk, preserves all the fields the system prompt names."""
    return "\n".join(
        json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
        for rec in corpus
    )


# ── Public API ────────────────────────────────────────────────────────
def find_specialists(
    user_query: str,
    specialty_metadata_collection: Collection,
) -> FindSpecialistsOutput:
    """Resolve a vernacular healthcare request to a ranked list of NUCC
    specialty codes.

    V5 LLM-picks pipeline: a single Pydantic-AI agent call against the
    full enriched NUCC corpus. Returns LLM-picked codes with LLM-judged
    relevance scores in [0,1], plus natural-language include/exclude
    audit paragraphs.
    """
    corpus = _load_corpus(specialty_metadata_collection)
    user_msg = _PICK_USER_TEMPLATE.format(
        user_query=user_query,
        n_records=len(corpus),
        corpus_block=_format_corpus(corpus),
    )
    result = run_llm_sync(
        _get_pick_agent(),
        user_msg,
        call_site="FindCare.SpecialtyFilter._pick_agent",
        provider="gemini",
        server="find_care",
        component="SpecialtyFilter",
    ).output

    # Defensive: filter any LLM-emitted code that isn't in the corpus
    # (the system prompt forbids invented codes, but enforce server-side).
    corpus_codes: frozenset[str] = frozenset(rec["Code"] for rec in corpus)
    cleaned_picks = [
        p for p in result.nucc_codes
        if p.code in corpus_codes and p.code not in _EXCLUDED_SET
    ]
    cleaned_picks.sort(key=lambda p: p.score, reverse=True)

    # ── Build the enriched `all_possible` union owned by the tool ────
    # List-one: the LLM picks, enriched with Display Name + flags from
    # SpecialtyMetaData. Lookup is keyed by Code; we project the same
    # fields the SpecialtyFilter CSS consumes.
    pick_codes = [p.code for p in cleaned_picks]
    meta_by_code: dict[str, dict[str, Any]] = {}
    if pick_codes:
        for d in specialty_metadata_collection.find(
            {"Code": {"$in": pick_codes}},
            {"_id": 0, "Code": 1, "Display Name": 1,
             "can_prescribe": 1, "homeopathic": 1},
        ):
            meta_by_code[d["Code"]] = d
    list_one: list[AllPossibleRecord] = []
    for i, p in enumerate(cleaned_picks):
        m = meta_by_code.get(p.code, {})
        list_one.append(AllPossibleRecord(
            code=p.code,
            name=m.get("Display Name", ""),
            can_prescribe=bool(m.get("can_prescribe", False)),
            homeopathic=bool(m.get("homeopathic", False)),
            homeopathic_general=False,
            rank=i,
        ))

    # List-two: the static homeopathic-generalist fallback set. Same
    # SpecialtyMetaData collection; selected by the homeopathic_general
    # flag. This set is query-independent.
    list_two: list[AllPossibleRecord] = []
    list_one_codes = {p.code for p in cleaned_picks}
    for i, d in enumerate(specialty_metadata_collection.find(
        {"homeopathic_general": True, "Section": "Individual"},
        {"_id": 0, "Code": 1, "Display Name": 1,
         "can_prescribe": 1, "homeopathic": 1},
    )):
        code = d.get("Code", "")
        if not code or code in list_one_codes:
            # A pick can also be a generalist; keep it in list-one only.
            continue
        list_two.append(AllPossibleRecord(
            code=code,
            name=d.get("Display Name", ""),
            can_prescribe=bool(d.get("can_prescribe", False)),
            homeopathic=True,
            homeopathic_general=True,
            rank=len(list_one) + i,
        ))

    return FindSpecialistsOutput(
        include=result.include,
        exclude=result.exclude,
        nucc_codes=cleaned_picks,
        all_possible=list_one + list_two,
    )
