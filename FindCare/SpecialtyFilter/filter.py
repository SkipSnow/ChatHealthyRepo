# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SpecialtyFilter — EPIC-006-F-002
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
    raise _ch_exc()(
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
        # OpenAI client created on first use (so the class can be instantiated
        # at module-load time before env vars are set).
        self._oai = None
        # Lazily loaded so the class can be instantiated even if prompts.json
        # is briefly unreachable; the first find_specialties() call will load.
        self._normalize_prompt: Optional[str] = None
        self._filter_prompt: Optional[str] = None

    def _ensure_openai_client(self):
        if self._oai is None:
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                raise _ch_exc()(
            mode="runtime_error",
            component="filter",
            message="OPENAI_API_KEY missing from environment")
            from openai import OpenAI
            self._oai = OpenAI(api_key=api_key)
        return self._oai

    # ── private prompt loaders ──────────────────────────────────────────────
    def _ensure_prompts_loaded(self) -> None:
        if self._normalize_prompt is None:
            self._normalize_prompt = load_prompt_text(NORMALIZE_RECORD_ID)
        if self._filter_prompt is None:
            self._filter_prompt = load_prompt_text(FILTER_RECORD_ID)

    # ── private pipeline steps ──────────────────────────────────────────────
    # Per EPIC-006-F-002-S-001-REQ-T-001 ("no fallback"), every stage MUST
    # fail loudly with the actual upstream cause. No silent degradation,
    # no swallowed exceptions, no substitute values. find_specialties()
    # surfaces the real reason in {"error": ...}.
    def normalize_query(self, raw_query: str) -> str:
        """Stage 1: translate vernacular healthcare request into NUCC-aligned
        text. Raises on any failure (no fallback per REQ-T-001)."""
        self._ensure_prompts_loaded()
        self._ensure_openai_client()
        r = self._oai.chat.completions.create(
            model=self._normalize_model, max_tokens=200,
            messages=[
                {"role": "system", "content": self._normalize_prompt},
                {"role": "user", "content": raw_query},
            ],
        )
        text = (r.choices[0].message.content or "").strip()
        if not text:
            raise _ch_exc()(
            mode="runtime_error",
            component="filter",
            message=f"normalize step returned empty text from model "
                f"{self._normalize_model!r} for query {raw_query!r}")
        log.info("normalize: %r -> %r", raw_query, text)
        return text

    def embed_query(self, text: str) -> list[float]:
        """Stage 2: embed the normalized text with the canonical model
        (declared at EPIC-008-F-011-S-004-REQ-B-001). Raises if the
        injected embedding function returns no vector (no fallback)."""
        qvec = self._get_vector(text)
        if not qvec:
            raise _ch_exc()(
            mode="runtime_error",
            component="filter",
            message="embedding step returned no vector — upstream embedding "
                "client failed (see container logs for the OpenAI/HTTP "
                "error). This is fatal per REQ-T-001 (no fallback).")
        return qvec

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
                          "Grouping": 1,
                          "can_prescribe": 1, "homeopathic": 1,
                          "score": {"$meta": "vectorSearchScore"}}},
        ]))
        return [r for r in rows
                if not (r.get("Display Name") or "").startswith("Deactivated")
                and r.get("score", 0) >= CAND_FLOOR]

    def filter_candidates(self, candidates: list[dict],
                          raw_query: str, normalized: str) -> list[str]:
        """Stage 4: LLM filter. Returns the final list of NUCC codes."""
        self._ensure_prompts_loaded()
        if not candidates:
            return []
        cand_block = "\n".join(
            f"{r['Code']} | score={r.get('score', 0):.4f} | "
            f"{r.get('Display Name', '')} | Grouping: {r.get('Grouping', '')}"
            for r in candidates
        )
        user_msg = (
            f"User request: {raw_query!r}\n\n"
            f"Normalized request (from upstream LLM): {normalized!r}\n\n"
            f"Candidate NUCC entries (highest cosine first):\n{cand_block}\n"
        )
        kwargs = dict(
            model=self._filter_model,
            messages=[
                {"role": "system", "content": self._filter_prompt},
                {"role": "user", "content": user_msg},
            ],
        )
        # gpt-5.x family rejects max_tokens; use max_completion_tokens
        if self._filter_model.startswith("gpt-5"):
            kwargs["max_completion_tokens"] = 400
        else:
            kwargs["max_tokens"] = 400
        self._ensure_openai_client()
        r = self._oai.chat.completions.create(**kwargs)
        raw = (r.choices[0].message.content or "").strip()
        # Parse codes — comma or newline separated, NONE means empty
        raw = raw.replace("\n", ",").replace(";", ",")
        codes = [c.strip() for c in raw.split(",")
                 if c.strip() and c.strip().upper() != "NONE"]
        # Defensive: keep only codes actually in the candidate set
        valid = {c["Code"] for c in candidates}
        return [c for c in codes if c in valid]

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
            normalized = self.normalize_query(query_text)
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
                "homeopathic": doc.get("homeopathic", False),
                "rank": rank,
            })
        log.info("filter: query=%r -> %d kept (from %d candidates)",
                  raw_query, len(specialties), len(candidates))
        return {"specialties": specialties}
