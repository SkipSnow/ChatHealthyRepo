# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# find_specialists — the FindCare specialty filter tool.
#
# Two-LLM-call pipeline:
#   1. Normalize agent (Gemini Flash Lite, cheap rewrite step) emits
#      `NormalizedQuery { include, exclude }` — both required, both
#      ≥50 chars, contrastive precision lever.
#   2. Embed include + exclude with OpenAI text-embedding-3-large,
#      compute include_vec − λ·exclude_vec, run Atlas $vectorSearch
#      against SpecialtyMetaData (Section: Individual + Code $nin
#      static admin-exclusion list), return top-30 candidates with
#      cosine scores.
#   3. Filter agent (GPT-4.1-mini, performance-tier model) walks the
#      30 candidates with the user query + normalize criteria and
#      drops the ones that share surface vocabulary but don't
#      actually match the clinical intent — the precision lever
#      vector search alone can't provide.
from __future__ import annotations

import os
from typing import Any

from openai import OpenAI
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pymongo.collection import Collection


# ── Pydantic I/O contracts ────────────────────────────────────────────
class NormalizedQuery(BaseModel):
    """Stage-1 output: positive + negative query strings for the
    contrastive embedding."""
    include: str = Field(..., min_length=50,
                         description="Positive query for the embedding (≥50 chars)")
    exclude: str = Field(..., min_length=50,
                         description="Negative query subtracted from include (≥50 chars; substantive contrastive signal)")


class NuccCodePick(BaseModel):
    """One NUCC code retained after the filter step."""
    code: str = Field(..., min_length=10, max_length=10,
                      description="NUCC 10-character alphanumeric code")
    score: float = Field(..., ge=0.0, le=1.0,
                         description="Final relevance score after filter [0.0, 1.0]")


class _FilterOutput(BaseModel):
    """Stage-3 output (internal): the filtered list of NUCC picks."""
    nucc_codes: list[NuccCodePick] = Field(..., min_length=0)


class FindSpecialistsOutput(BaseModel):
    """Tool output. Single list of NUCC matches (with `score` reflecting
    the filter's refined relevance), plus `include`/`exclude` audit
    fields capturing the Stage-1 reasoning.
    """
    include: str
    exclude: str
    nucc_codes: list[NuccCodePick]


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


# ── Defaults ──────────────────────────────────────────────────────────
NORMALIZE_MODEL_ID = "google-gla:gemini-3.1-flash-lite-preview"
FILTER_MODEL_ID = "openai:gpt-4.1-mini"
EMBED_MODEL = "text-embedding-3-large"
LAMBDA = 0.5
NUM_CANDIDATES = 800
TOP_K_RETRIEVAL = 30


# ── Stage 1: normalize prompt ─────────────────────────────────────────
_NORMALIZE_PROMPT = """
You are part of a healthcare-provider directory search.

PURPOSE — what your output is used for:
The user has typed a vernacular healthcare request. Your job is to
translate that request into TWO short pieces of text:
  - `include`: the POSITIVE query, embedded as the search vector
    against an enriched 'NUCC' database derived from
    https://www.nucc.org/index.php/code-sets-mainmenu-41/provider-taxonomy-mainmenu-40
  - `exclude`: the NEGATIVE query, whose embedding is SUBTRACTED
    from the include vector to push the search away from off-target
    specialties.
We are optimizing for EXCELLENT RECALL AND PRECISION on that
semantic search. We only care about INDIVIDUAL HUMAN PRACTITIONERS
in the result set; institutions, agencies, facilities, clinics,
suppliers, and other non-individual NUCC entries are filtered out
at retrieval time.

WHAT YOU ARE MATCHING AGAINST:
The corpus is the NUCC provider taxonomy. Each record is embedded
from a string of the shape
    "Classification | Specialization | Display Name | Definition"

IMPORTANT — `Grouping` and `Classification` are HINTS, not
determinative. The user's clinical intent can be served by records
whose grouping has nothing to do with the words the user used. For
example, "Pediatric Surgery Physician" sits under
`Classification: Surgery`, NOT `Pediatrics`. Anchor on intent.

INFER GENDER AND AGE WHEN POSSIBLE:
If the user's wording lets you infer the patient's gender or age
(e.g. "my husband's prostate" → adult male; "kid's doc" → pediatric
population; "for my mom, she's 78" → elderly female), factor those
into your include and exclude reasoning. If gender or age is NOT
inferable, do NOT invent either.

CAM / HOLISTIC SAFEGUARD:
Do NOT push away from complementary, alternative, integrative, or
holistic medicine providers in your `exclude` (acupuncturists,
naturopaths, chiropractors, naprapaths, homeopaths, mechanotherapists,
massage therapists, midwives, etc.). They are part of the corpus,
are licensed in many U.S. jurisdictions, and may be the right
answer. Let the embedding decide.

OUTPUT — strict, typed: a JSON object with exactly two string
fields, `include` and `exclude`. BOTH FIELDS ARE REQUIRED and BOTH
MUST BE SUBSTANTIVE — at least 50 characters each. The reason for
this is PRECISION: the contrastive signal between include and
exclude is what lets the embedding distinguish near-neighbor
specialties that share surface vocabulary. A short or empty
exclude collapses the search back to plain cosine similarity over
the include alone, which is exactly the imprecise behavior we are
trying to fix. A meaty, specific exclude is how we earn precision.
No preamble, no markdown fences, no explanation.

NORMALIZATION RULES for `include`:
- Use authentic medical vocabulary — terms that appear in NUCC
  Definitions for the specialties that match the user's intent.
- Keep the meaningful clinical context the user provided.

NORMALIZATION RULES for `exclude`:
- Name vernacular categories or wrong-context specialties that
  surface-vocabulary overlap would otherwise drag in.
- Substantive (≥50 chars). Honor the CAM safeguard above.
""".strip()


# ── Stage 3: filter prompt template (formatted per call) ──────────────
_FILTER_PROMPT_TEMPLATE = """
You are the precision filter for the FindCare specialty search.

A vector search has already returned the top-{top_k} candidates
for the user's request. Your job: walk that candidate list and
keep only the codes that actually match the user's clinical
intent. Drop the ones that scored well only because they share
surface vocabulary with the query but aren't a fit (e.g.
"sports medicine" appearing for a wrist-injury query, or
pediatric variants for an adult patient).

We are still optimizing for EXCELLENT RECALL — when in doubt
about a candidate, keep it with a lower score. Missing a true
match is worse than including a borderline one. The point of
this filter is to remove the OBVIOUSLY OFF-TARGET candidates,
not to second-guess every borderline call.

INFER GENDER AND AGE WHEN POSSIBLE — same rule as the normalize
step. If the user's wording implies adult, drop pediatric-only
variants; if elderly, drop pediatric; if female-specific, drop
male-specific; etc. If demographics are not inferable, keep age/
gender variants in.

CAM / HOLISTIC SAFEGUARD — do not drop CAM/holistic practitioners
just because they're CAM. Drop them only if they're clinically
off-target for the user's specific request.

OUTPUT — strict, typed: a JSON object with one field
`nucc_codes` — a list of objects, each {{ "code": <NUCC 10-char
code>, "score": <float 0.0–1.0> }}. Score reflects YOUR refined
judgment of relevance: 1.0 = exactly what they asked for, 0.7 =
strong adjacent option, 0.4 = borderline kept for recall. Order
by score, highest first. Use ONLY codes that appear in the
candidate list below — do not invent codes, do not emit codes
absent from the candidates.

USER QUERY:
{user_query}

NORMALIZE STAGE INCLUDE CRITERIA:
{include}

NORMALIZE STAGE EXCLUDE CRITERIA:
{exclude}

CANDIDATES (top-{top_k} from the vectorSearch, with cosine
scores):
{candidates_block}

Emit ONLY the JSON object. No preamble, no markdown fences, no
explanation.
""".strip()


# ── Module-level singletons ───────────────────────────────────────────
_normalize_agent: Agent | None = None
_filter_agent: Agent | None = None
_openai_client: OpenAI | None = None


def _get_normalize_agent() -> Agent:
    global _normalize_agent
    if _normalize_agent is None:
        if not os.getenv("GEMINI_API_KEY"):
            raise RuntimeError("GEMINI_API_KEY not set; required for find_specialists.")
        _normalize_agent = Agent(
            NORMALIZE_MODEL_ID,
            output_type=NormalizedQuery,
            system_prompt=_NORMALIZE_PROMPT,
            model_settings={"temperature": 0.3},
            retries=5,
        )
    return _normalize_agent


def _get_filter_agent() -> Agent:
    """Filter agent uses GPT-4.1-mini — performance-tier model for
    the precision step. Reasoning quality matters more here than
    cost/latency: the filter is what differentiates 'shares surface
    vocabulary' from 'actually matches clinical intent'."""
    global _filter_agent
    if _filter_agent is None:
        if not os.getenv("OPENAI_API_KEY"):
            raise RuntimeError("OPENAI_API_KEY not set; required for filter agent.")
        _filter_agent = Agent(
            FILTER_MODEL_ID,
            output_type=_FilterOutput,
            model_settings={"temperature": 0.2},
            retries=5,
        )
    return _filter_agent


def _get_openai() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        key = os.getenv("OPENAI_API_KEY")
        if not key:
            raise RuntimeError("OPENAI_API_KEY not set; required for embedding.")
        _openai_client = OpenAI(api_key=key)
    return _openai_client


def _embed(text: str) -> list[float]:
    return _get_openai().embeddings.create(
        model=EMBED_MODEL, input=text
    ).data[0].embedding


def _vec_combine(inc: list[float], exc: list[float], lam: float) -> list[float]:
    return [a - lam * b for a, b in zip(inc, exc)]


def _vector_search(col: Collection, qvec: list[float], top_k: int) -> list[dict[str, Any]]:
    pipeline: list[dict[str, Any]] = [
        {"$vectorSearch": {
            "index": "specialty_vector_index",
            "path": "embedding",
            "queryVector": qvec,
            "numCandidates": NUM_CANDIDATES,
            "limit": top_k,
            "filter": {
                "Section": "Individual",
                "Code": {"$nin": EXCLUDED_CODES},
            },
        }},
        {"$project": {
            "_id": 0,
            "Code": 1,
            "Display Name": 1,
            "Definition": 1,
            "score": {"$meta": "vectorSearchScore"},
        }},
    ]
    raw = list(col.aggregate(pipeline))
    return [h for h in raw
            if not (h.get("Display Name") or "").startswith("Deactivated")]


def _format_candidates(candidates: list[dict[str, Any]]) -> str:
    """One line per candidate, with cosine score, code, display name,
    and definition trimmed for the filter LLM's context."""
    lines = []
    for c in candidates:
        defn = (c.get("Definition") or "").strip().replace("\n", " ")
        lines.append(
            f"[{c.get('Code', '')}] cosine={c.get('score', 0):.4f} | "
            f"{c.get('Display Name', '')} | {defn}"
        )
    return "\n".join(lines)


# ── Public API ────────────────────────────────────────────────────────
def find_specialists(
    user_query: str,
    specialty_metadata_collection: Collection,
) -> FindSpecialistsOutput:
    """Resolve a vernacular healthcare request to a ranked list of NUCC
    specialty codes.

    Pipeline: Stage-1 normalize → embed + vectorSearch → Stage-3 filter.
    """
    nq = _get_normalize_agent().run_sync(user_query).output

    inc_vec = _embed(nq.include)
    exc_vec = _embed(nq.exclude)
    qvec = _vec_combine(inc_vec, exc_vec, LAMBDA)
    candidates = _vector_search(
        specialty_metadata_collection, qvec, TOP_K_RETRIEVAL
    )

    if not candidates:
        return FindSpecialistsOutput(
            include=nq.include, exclude=nq.exclude, nucc_codes=[],
        )

    filter_user_msg = _FILTER_PROMPT_TEMPLATE.format(
        top_k=TOP_K_RETRIEVAL,
        user_query=user_query,
        include=nq.include,
        exclude=nq.exclude,
        candidates_block=_format_candidates(candidates),
    )
    filtered = _get_filter_agent().run_sync(filter_user_msg).output

    return FindSpecialistsOutput(
        include=nq.include,
        exclude=nq.exclude,
        nucc_codes=filtered.nucc_codes,
    )
