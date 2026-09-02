# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SpecialtyFilter — EPIC-006-F-003
#
# Stage-1 normalize + embed + vector search + Stage-2 AI filter.
# Loaded as an in-process class by the FindCare app driver. No HTTP
# routes here, no public/protected interface. Per S-005-T-001 every
# method on this class is PRIVATE.
#
# System prompts are loaded from brain/machine_artifacts/content/
# prompts.json (records `specialty_normalize_system_prompt` and
# `specialty_filter_system_prompt`) per S-002-T-001 / T-005.

import json
from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import os
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from chathealthy_lib.llm import run_llm_sync
from chathealthy_lib.runtime_data_collections import specialty_meta_coll

# Resolve project root from this file's location:
#   FindCare/SpecialtyFilter/filter.py
#   parents: [0]SpecialtyFilter [1]FindCare [2]<project root>

log = ChatHealthyLoggingService()


PROJECT_ROOT = Path(__file__).resolve().parents[2]
PROMPTS_JSON_PATH = PROJECT_ROOT / "brain" / "machine_artifacts" / "content" / "prompts.json"
NORMALIZE_RECORD_ID = "specialty_normalize_system_prompt"
FILTER_RECORD_ID = "specialty_filter_system_prompt"

# Cosine candidate floor passed to Stage-2 filter (S-002-T-004; floor
# value is a design choice, not a REQ). 0.55 picked from Phase A/C/D
# evidence as the high-recall handoff that gives Stage-2 enough pool
# to refine without flooding the LLM.
CAND_FLOOR = 0.55

UNLICENSED_GROUPS = {"Other Service Providers", "Student, Health Care"}


class NormalizedRequest(BaseModel):
    """Stage 1's answer. Typed, so a malformed answer is rejected by
    pydantic-ai and re-asked rather than hand-parsed here."""
    search_term: str = Field(
        description="The NUCC-aligned text to search specialties with.")
    complaint: str = Field(
        default="",
        description="The same request restated as the kind of problem the "
                    "person has, in two to four words of plain clinical "
                    "language.")


class KeptCodes(BaseModel):
    """Stage 4's answer: the NUCC codes the request implies."""
    codes: list[str] = Field(
        default_factory=list,
        description="NUCC taxonomy codes drawn from the candidate set. "
                    "Empty when the request implies none of them.")



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


def load_prompt_text(record_id: str) -> str:
    """Read prompts.json once and pull the system_prompt for `record_id`."""
    with PROMPTS_JSON_PATH.open(encoding="utf-8") as f:
        d = json.load(f)
    for r in d.get("records", []):
        if r.get("_record_id") == record_id:
            return r.get("system_prompt", "")
    raise ChatHealthyException(
            mode="key_error",
            component="filter",
            message=f"prompts.json: no record with _record_id={record_id!r}")


class SpecialtyFilter:
    """In-process specialty filter for FindCare.

    Pipeline (all internal — no HTTP):
      1. normalize_query(text, geo)        — LLM normalize via Stage-1 prompt
      2. embed_query(text)                 — text-embedding-3-large
      3. vector_search(qvec)               — Atlas $vectorSearch on
                                              SpecialtyMetaData (Section=Individual)
      4. filter_candidates(cands, query)   — LLM filter via Stage-2 prompt
      5. find_specialties(query, geo, ...) — orchestration; calls 1→4 once,
                                              returns the kept code list

    The class has no run, no main, no entry point. The driver (app.py)
    instantiates one and calls find_specialties() from a single route
    handler.
    """

    def __init__(self, get_db_fn, env_prefix: str, get_vector_fn, *,
                 normalize_model: str = "gpt-4.1-mini",
                 filter_model: str = "gpt-5.4-nano"):
        """
        Args:
            get_db_fn       : callable returning MongoDB client (or None)
            env_prefix      : 'dev'/'qa'/'prod'
            get_vector_fn   : callable(text) -> list[float] embedding
            normalize_model : model id for Stage-1 (loaded from prompts.json record)
            filter_model    : model id for Stage-2 (loaded from prompts.json record)
        """
        self._get_db = get_db_fn
        self._env = env_prefix
        self._get_vector = get_vector_fn
        self._normalize_model = normalize_model
        self._filter_model = filter_model
        # Two agents, because the two stages are two different asks with two
        # different prompts and two different output shapes. What they stop
        # keeping is a client, a credential, a retry policy and a vendor
        # quirk -- all of that is the facade's.
        self._normalize_agent = None
        self._filter_agent = None
        # Lazily loaded so the class can be instantiated even if prompts.json
        # is briefly unreachable; the first find_specialties() call will load.
        self._normalize_prompt: Optional[str] = None
        self._filter_prompt: Optional[str] = None

    def _ensure_normalize_agent(self):
        if self._normalize_agent is None:
            from pydantic_ai import Agent, ModelRetry
            agent = Agent(
                f"openai:{self._normalize_model}",
                output_type=NormalizedRequest,
                system_prompt=self._normalize_prompt + self._DUAL_INSTRUCTION,
            )

            @agent.output_validator
            def _require_search_term(result: NormalizedRequest) -> NormalizedRequest:
                if not result.search_term.strip():
                    raise ModelRetry(
                        "search_term was empty. Return the NUCC-aligned text "
                        "to search specialties with.")
                return result

            self._normalize_agent = agent
        return self._normalize_agent

    def _ensure_filter_agent(self):
        if self._filter_agent is None:
            from pydantic_ai import Agent
            self._filter_agent = Agent(
                f"openai:{self._filter_model}",
                output_type=KeptCodes,
                system_prompt=self._filter_prompt,
            )
        return self._filter_agent

    # ── private prompt loaders ──────────────────────────────────────────────
    def _ensure_prompts_loaded(self) -> None:
        if self._normalize_prompt is None:
            self._normalize_prompt = load_prompt_text(NORMALIZE_RECORD_ID)
        if self._filter_prompt is None:
            self._filter_prompt = load_prompt_text(FILTER_RECORD_ID)

    # ── private pipeline steps ──────────────────────────────────────────────
    # Per EPIC-006-F-003-S-001-REQ-T-001 ("no fallback"), every stage MUST
    # fail loudly with the actual upstream cause. No silent degradation,
    # no swallowed exceptions, no substitute values. find_specialties()
    # surfaces the real reason in {"error": ...}.
    # One utterance normalizes two ways, for two different consumers.
    #
    #   search term  what to look for   "orthodontist"
    #   complaint    what is wrong      "tooth problem"
    #
    # They are not interchangeable. The search term is the right answer for
    # the embedding and the specialty filter, and the wrong answer for the
    # user-facing complaint: "orthodontist" is who you see, not what you
    # have. Emitting one string and using it for both put a specialty name
    # in the complaint field.
    _DUAL_INSTRUCTION = """

Return STRICT JSON with exactly two keys and nothing else:
  {"search_term": "...", "complaint": "..."}

search_term: the NUCC-aligned text to search specialties with, exactly as
instructed above. This is what gets embedded.

complaint: the same request restated as the KIND OF PROBLEM the person
has, in two to four words of plain clinical language. Never a specialty
name, never the words they used, never a place.

  "find me a shrink in Long Beach CA" -> search_term "psychiatrist",
                                         complaint "psychological problem"
  "I need an orthodontist"            -> search_term "orthodontist",
                                         complaint "tooth problem"
  "find me a bone doc in Seattle WA"  -> search_term "orthopedic surgeon",
                                         complaint "bone problem"
"""

    def normalize_query(self, raw_query: str) -> tuple[str, str]:
        """Stage 1: (search_term, complaint) from the vernacular request.

        Raises on any failure (no fallback per REQ-T-001).
        """
        self._ensure_prompts_loaded()
        result = run_llm_sync(
            self._ensure_normalize_agent(), raw_query,
            call_site="SpecialtyFilter.normalize_query",
            provider="openai", server="find_care", component="SpecialtyFilter")
        return result.output.search_term.strip(), result.output.complaint.strip()

    def embed_query(self, text: str) -> list[float]:
        """Stage 2: embed the normalized text with the canonical model
        (declared at EPIC-008-F-011-S-004-REQ-B-001). Raises if the
        injected embedding function returns no vector (no fallback)."""
        return self._get_vector(text)

    @staticmethod
    def _is_inactive(row: dict) -> bool:
        return "inactive" in (row.get("Notes") or "").lower()

    def vector_search(self, qvec: list[float]) -> list[dict]:
        """Stage 3: $vectorSearch SpecialtyMetaData. Returns ALL candidates
        with cosine ≥ _CAND_FLOOR, dropping Deactivated entries."""
        db = self._get_db()
        if db is None:
            return []
        coll = specialty_meta_coll()
        total = coll.count_documents({"Section": "Individual"})
        rows = list(coll.aggregate([
            {"$vectorSearch": {
                "index": "specialty_vector_index",
                "path": "embedding",
                "queryVector": qvec,
                "numCandidates": total,
                "limit": total,
                "filter": {"Section": "Individual"},
            }},
            {"$project": {"_id": 0, "Code": 1, "Display Name": 1,
                          "Grouping": 1, "Classification": 1,
                          "Specialization": 1, "Notes": 1,
                          "can_prescribe": 1, "is_homeopathic": 1,
                          "score": {"$meta": "vectorSearchScore"}}},
        ]))
        return [r for r in rows
                if not (r.get("Display Name") or "").startswith("Deactivated")
                and not self._is_inactive(r)
                and r.get("score", 0) >= CAND_FLOOR]

    def filter_candidates(self, candidates: list[dict],
                          raw_query: str, normalized: str) -> list[str]:
        """Stage 4: LLM filter. Returns the final list of NUCC codes."""
        self._ensure_prompts_loaded()
        if not candidates:
            return []
        # Every field the record carries that bears on the choice. NUCC gives
        # two codes the same Display Name in six cases, so a candidate line
        # built from that field alone offers the model the same words twice
        # and it has nothing to choose on. Specialization and Definition are
        # what separate them, and they are already what the embedding was
        # composed from.
        cand_block = "\n".join(
            " | ".join(part for part in (
                r["Code"],
                f"score={r.get('score', 0):.4f}",
                r.get("Display Name", ""),
                f"Grouping: {r['Grouping']}" if r.get("Grouping") else "",
                f"Definition: {r['Definition']}" if r.get("Definition") else "",
            ) if part)
            for r in candidates
        )
        user_msg = (
            f"User request: {raw_query!r}\n\n"
            f"Normalized request (from upstream LLM): {normalized!r}\n\n"
            f"Candidate NUCC entries (highest cosine first):\n{cand_block}\n"
        )
        result = run_llm_sync(
            self._ensure_filter_agent(), user_msg,
            call_site="SpecialtyFilter.filter_candidates",
            provider="openai", server="find_care", component="SpecialtyFilter")
        # The model chooses among registry facts and cannot invent one: its
        # answer is intersected with the candidate set the catalogue
        # actually returned. That intersection is what makes the semantic
        # step safe to have at all.
        valid = {c["Code"] for c in candidates}
        return [c for c in result.output.codes if c in valid]

    # ── public-to-driver orchestration ──────────────────────────────────────
    def find_specialties(self, raw_query: str,
                         chat_history: Optional[list[str]] = None) -> dict:
        """End-to-end: normalize → embed → vector search → AI filter.
        Returns the structured payload the driver returns to the frontend.
        The driver decides what HTTP route exposes this — this class never
        sees an HTTP request.

        Per REQ-T-001 (no fallback) every stage fails loudly. This method
        is the SINGLE catch point — any stage exception is converted into
        {"error": "<stage>: <type>: <message>"} so the driver can return
        the actual upstream cause to the frontend instead of a generic
        'something failed' string. Logs include the same context plus a
        traceback for the operator."""
        # Build the query text from chat history + current query
        parts = []
        if chat_history:
            parts.extend(chat_history[-5:])
        parts.append(raw_query)
        query_text = " ".join(parts)

        try:
            normalized, complaint = self.normalize_query(query_text)
        except Exception as exc:
            # Mode 2 (REQ-B-008): Stage-1 LLM normalize failed; user gets
            # graceful error dict that the tool surfaces back to the user.
            # No 503. Operator MUST know.
            log.error("Stage 1 normalize failed for %r", raw_query, exc=ChatHealthyException(
                                                                         mode="specialty_filter_stage1_normalize_failed",
                                                                         message=f"Stage 1 normalize failed for {raw_query!r}: {exc}",
                                                                         component="SpecialtyFilter",
                                                                         exception=exc,
                                                                     ), if_not_debug_log=True)
            return {"error": f"normalize: {type(exc).__name__}: {exc}"}

        try:
            qvec = self.embed_query(normalized)
        except Exception as exc:
            # Mode 2 (REQ-B-008): Stage-2 embed failed; user gets graceful
            # error dict. Embedding infrastructure issue — operator MUST know.
            log.error("Stage 2 embed failed for normalized=%r", normalized, exc=ChatHealthyException(
                                                                                 mode="specialty_filter_stage2_embed_failed",
                                                                                 message=f"Stage 2 embed failed for normalized={normalized!r}: {exc}",
                                                                                 component="SpecialtyFilter",
                                                                                 exception=exc,
                                                                             ), if_not_debug_log=True)
            return {"error": f"embed: {type(exc).__name__}: {exc}"}

        try:
            candidates = self.vector_search(qvec)
        except Exception as exc:
            # Mode 2 (REQ-B-008): Stage-3 vector search failed; user gets
            # graceful error dict. Atlas $vectorSearch infra issue —
            # operator MUST know.
            log.error("Stage 3 vector_search failed", exc=ChatHealthyException(
                                                           mode="specialty_filter_stage3_vector_search_failed",
                                                           message=f"Stage 3 vector_search failed: {exc}",
                                                           component="SpecialtyFilter",
                                                           exception=exc,
                                                       ), if_not_debug_log=True)
            return {"error": f"vector_search: {type(exc).__name__}: {exc}"}
        if not candidates:
            return {"specialties": [],
                    "message": f"No matching specialty found for {raw_query!r}."}

        try:
            kept_codes = self.filter_candidates(candidates, raw_query, normalized)
        except Exception as exc:
            # Mode 2 (REQ-B-008): Stage-4 LLM filter failed; user gets
            # graceful error dict. LLM provider issue — operator MUST know.
            log.error("Stage 4 filter_candidates failed", exc=ChatHealthyException(
                                                               mode="specialty_filter_stage4_filter_candidates_failed",
                                                               message=f"Stage 4 filter_candidates failed: {exc}",
                                                               component="SpecialtyFilter",
                                                               exception=exc,
                                                           ), if_not_debug_log=True)
            return {"error": f"filter: {type(exc).__name__}: {exc}"}
        if not kept_codes:
            return {"specialties": [],
                    "message": f"No matching specialty found for {raw_query!r}."}

        # Build the response shape the frontend expects
        kept_set = set(kept_codes)
        kept = [c for c in candidates if c["Code"] in kept_set]
        # Re-rank by cosine (S-002-T-009: rank 1..n preserved)
        kept.sort(key=lambda x: -x.get("score", 0))
        specialties = []
        for rank, doc in enumerate(kept, start=1):
            specialties.append({
                "Code": doc["Code"],
                "Display Name": doc.get("Display Name", ""),
                "can_prescribe": doc.get("can_prescribe", False),
                "homeopathic": doc.get("is_homeopathic", False),
                "rank": rank,
            })
        log.info("filter: query=%r -> %d kept (from %d candidates)",
                  raw_query, len(specialties), len(candidates))
        return {"specialties": specialties, "complaint": complaint}
