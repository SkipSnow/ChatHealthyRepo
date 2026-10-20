# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""FDADrugEnrichmentTool (EPIC-010-F-007-S-008) -- the production inference that
builds the fdaProductToDiagnosisResolution collection (the FDA drug collection).

One claims-blind pydantic-ai agent making TWO single-concern calls, never merged:

  PROMPT 1 -- the RXCUI resolution (the prerequisite, runs FIRST). Each FDA drug
  is resolved to its RxNorm ingredient RXCUI(s) (tty=IN) off the label's own
  openfda.rxcui through an ordered funnel. A combination drug resolves to the
  full set of its tty=IN ingredients, a single-ingredient drug to a one-element
  set:
    tier1  deterministic RxNorm, EXACT   -- the label's rxcui related directly to
           its tty=IN ingredient(s) (no model).
    tier2  deterministic RxNorm, NORMALIZED -- the label's rxcui normalized via
           RxNorm status/remap, then related to its tty=IN ingredient(s)
           (no model).
    tier3  bounded inference on the residue ONLY -- RxNav's approximate match
           supplies the real candidate RXCUIs for the unresolved name; the model
           SELECTS one or declines. It never invents an identifier.
    floor  unresolvable, reported by reason.
  tier1/tier2 carry confidence=1 and confidence_rationale="Deterministic".
  Its output is RxcuiResolutionOutput.

  PROMPT 2 -- the crosswalk. Indication text -> ICD-10-CM 3/4-char codes chosen
  ONLY from the catalog. The rxcui PROMPT 1 resolved is INSERTED into PROMPT 2's
  input only when that resolution's confidence > 0.75 (tier1/2 always; tier3 only
  above 0.75; floor and any tier3 at or below 0.75 never). Its output assembles
  the FdaDrugRecord -- the FDA SPL record enriched in place.

The agent sees only strings. RxNorm lookups use the RxNav REST API. The model is
resolved from configuration (never pinned in source) and constructed per the per-
provider call shape (Appendix D). The sink is pluggable -- local disk for the
benchmark, MongoDB in production -- and is the caller's, not the tool's: this
module opens no database. No model call happens on import or construction; a call
fires only when resolve_rxcuis(), crosswalk(), or enrich() is invoked.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Iterable, Literal, Optional

import httpx
from pydantic import BaseModel, ConfigDict, Field

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (resolved from the environment; never pinned in source)
# ─────────────────────────────────────────────────────────────────────────────
_MODEL_ENV = "CH_FDA_ENRICHMENT_MODEL"          # e.g. "gpt-5.6-luna"
_CATALOG_ENV = "CH_ICD10_CATALOG_PATH"          # path to the ICD-10-CM catalog xlsx
_RXNAV_ENV = "CH_RXNAV_BASE_URL"                # optional override of the public API

_RXNAV_DEFAULT_BASE = "https://rxnav.nlm.nih.gov/REST"
_CROSSWALK_BATCH_SIZE = 25                      # the production batch size (50 -> 25, operator ruling 2026-10-20)
_APPROX_MAX_CANDIDATES = 20                     # bounded tier-3 candidate list
_INSERT_THRESHOLD = 0.75                        # rxcui inserted into PROMPT 2 only above this
_HTTP_TIMEOUT_SECONDS = 30.0
_MAX_TRANSIENT_RETRIES = 5
_MODEL_MAX_TOKENS = 32000                       # headroom for a full 50-record batch

# Per-provider call shape, Appendix D. The model STRING comes from configuration;
# the construction mechanics per provider live here because pydantic-ai needs the
# right model class and max-effort setting for each vendor's API.
_MODEL_REGISTRY: dict[str, dict[str, str]] = {
    "gpt-5.6-luna":          {"provider": "openai", "effort": "xhigh"},
    "gemini-3.5-flash-lite": {"provider": "google", "thinking_level": "high"},
    "gemini-3.8-flash":      {"provider": "google", "thinking_level": "high"},
    "grok-4.3":              {"provider": "xai",    "effort": "high"},
}

_TRANSIENT_STATUS = frozenset({429, 500, 502, 503, 504, 529})

# The rationale stamped on a residue name the tier-3 model OMITTED from its batch
# output (a drop, not a decline). A model that declines carries its own rationale;
# a dropped id gets this sentinel so the caller can tell a drop from a decline and
# refire only the dropped ids per the retest-on-drop protocol (Appendix/section 5).
_RESOLUTION_DROP_RATIONALE = (
    "No RxNorm candidate selected by the resolver; name is unresolvable.")

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT 1 -- the verbatim drug-name -> RXCUI resolution system prompt (Appendix C)
# ─────────────────────────────────────────────────────────────────────────────
_PROMPT_1_SYSTEM = (
    "You are an expert pharmacist and drug-terminology specialist. You are given a batch of "
    "drug names taken from FDA drug labels whose deterministic RxNorm resolution -- exact and "
    "normalized, off the label's own rxcui -- could not resolve. For each name you are also "
    "given the candidate RxNorm concepts that RxNorm's approximate match returned for it: each "
    "candidate is a real RxNorm RXCUI with its RxNorm name, term type, and ingredient. Your task "
    "is to SELECT the candidate that is the same drug as the input name. You never invent an "
    "identifier; you only choose among the candidates given, or decline. "
    "For each input name return: rxcui -- the RXCUI of the candidate you selected, copied exactly "
    "from the candidate list, or null if none is the same drug; ingredient_rxcui -- the RxNorm "
    "ingredient RXCUI (tty=IN) of the selected concept, taken from the candidate as given, or "
    "null; confidence -- 0 to 1; confidence_rationale -- one plain sentence explaining why you "
    "selected that candidate, or why none matched. "
    "Absolute rules, because this output is auditable and may become evidence: Choose ONLY from "
    "the candidate RXCUIs provided -- never emit an RXCUI that is not in the candidate list, and "
    "never invent, alter, or guess a number. If no candidate is the same drug, return null -- a "
    "null correctly passes the name to the unresolvable floor; a wrong identifier is far worse "
    "than an honest null. Be honest and conservative with confidence: reserve high confidence for "
    "a clear, unambiguous match, and do not inflate it -- not braggadocious. Return one result "
    "per input name, keyed by its id."
)

# ─────────────────────────────────────────────────────────────────────────────
# PROMPT 2 -- the verbatim crosswalk system prompt (Appendix C). The ICD-10-CM
# catalog is appended as the cached static prefix at build time.
# ─────────────────────────────────────────────────────────────────────────────
_PROMPT_2_INSTRUCTIONS = (
    "You are an expert clinical coder. You are given a batch of up to 50 records in the input "
    "field named \"drugs\" (named so for model familiarity). Each element is ONE record: one "
    "SPL's FDA \"Indications and Usage\" text. \"drug\" here means one record -- one label -- not a "
    "marketed product. For each record, read its Indications and Usage text and produce the list "
    "of distinct indications it is indicated to treat. "
    "For each indication, list ALL of its ICD-10-CM codes at the 3-character and 4-character "
    "levels, chosen ONLY from the catalog below -- never emit a code that is not in the catalog. "
    "Give each code a confidence from 0 to 1, and give each indication a short "
    "confidence_rationale: one plain sentence explaining that confidence. A best guess is "
    "acceptable. Be honest and conservative with confidence: reserve high confidence for "
    "genuinely clear cases, and do not inflate it -- not braggadocious. "
    "Also return, per record, a usage_summary: a concise summary of the clinical usage context in "
    "the Indications and Usage text that is distinct from the indication itself (target "
    "populations, limitations of use), with document noise (figure captions, trial statistics) "
    "omitted. Return one result per input record, keyed by its id."
)

_CATALOG_HEADER = (
    "ICD-10-CM catalog - each line is  <icd10_code><TAB><short_description>  (no decimal point; "
    "e.g. F32 or F329). All 12,014 level-3 and level-4 codes follow, one per line; cached prefix."
)


# ─────────────────────────────────────────────────────────────────────────────
# Schemas -- Appendix B
# ─────────────────────────────────────────────────────────────────────────────
class DrugInput(BaseModel):
    """One record in CrosswalkBatchInput (Appendix B). additionalProperties:false.
    rxcui is OPTIONAL -- the funnel-resolved ingredient RXCUI, inserted only when
    the resolution confidence > 0.75; absent (empty) otherwise. A hint, never the key."""
    model_config = ConfigDict(extra="forbid")
    id: str
    set_id: str
    molecule: str
    rxcui: list[str] = Field(default_factory=list)
    indication_text: str


class CrosswalkBatchInput(BaseModel):
    """PROMPT 2 input (Appendix B)."""
    model_config = ConfigDict(extra="forbid")
    drugs: list[DrugInput] = Field(min_length=1, max_length=50)


class RxcuiResolutionItem(BaseModel):
    """One resolution record (Appendix B). rxcui is null and ingredient_rxcuis is
    empty and resolved_by is "floor" when the drug cannot be resolved. A
    combination drug resolves to the full set of its tty=IN ingredient RXCUIs; a
    non-empty array is a resolved join."""
    model_config = ConfigDict(extra="forbid")
    id: str
    rxcui: Optional[str]
    ingredient_rxcuis: list[str] = Field(default_factory=list)
    ingredient_names: list[str] = Field(default_factory=list)
    resolved_by: Literal["tier1", "tier2", "tier3", "floor"]
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_rationale: str


class RxcuiResolutionOutput(BaseModel):
    """PROMPT 1 output (Appendix B) -- the prerequisite that feeds PROMPT 2."""
    model_config = ConfigDict(extra="forbid")
    results: list[RxcuiResolutionItem]


class Tier3Selection(BaseModel):
    """The tier-3 model's selection for one unresolved name -- exactly what the
    PROMPT 1 system prompt (Appendix C) asks the model to return. The orchestrator
    stamps resolved_by (tier3 when rxcui is non-null, floor when the model declines
    with null) to assemble the RxcuiResolutionItem above."""
    model_config = ConfigDict(extra="forbid")
    id: str
    rxcui: Optional[str]
    ingredient_rxcui: Optional[str]
    confidence: float = Field(ge=0.0, le=1.0)
    confidence_rationale: str


class Tier3SelectionOutput(BaseModel):
    """PROMPT 1's tier-3 model output_type -- one selection per residue name."""
    model_config = ConfigDict(extra="forbid")
    results: list[Tier3Selection]


class CandidateConcept(BaseModel):
    """One real RxNorm concept RxNav's approximate match returned for an unresolved
    name: its RXCUI, RxNorm name, term type, and every tty=IN ingredient related to
    it (a combination concept carries more than one)."""
    rxcui: str
    name: str
    tty: str
    ingredient_rxcuis: list[str] = Field(default_factory=list)
    ingredient_names: list[str] = Field(default_factory=list)


class UnresolvedDrug(BaseModel):
    """A residue drug name plus the candidate concepts the model chooses among."""
    id: str
    name: str
    candidates: list[CandidateConcept]


class ResolutionBatchInput(BaseModel):
    """PROMPT 1 tier-3 typed input (deps)."""
    drugs: list[UnresolvedDrug]


class Icd10Code(BaseModel):
    """One crosswalk code (Appendix B). level marks 3- vs 4-character."""
    model_config = ConfigDict(extra="forbid")
    icd10_code: str
    level: Literal[3, 4]
    confidence: float = Field(ge=0.0, le=1.0)
    description: Optional[str] = None


class CrosswalkIndication(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indication: str
    confidence_rationale: str
    codes: list[Icd10Code]


class CrosswalkResult(BaseModel):
    """One record's crosswalk output -- id, usage_summary, and the indications with
    their catalog codes. The carried resolution fields and set_id/record_number are
    added by the orchestrator when it assembles the FdaDrugRecord."""
    model_config = ConfigDict(extra="forbid")
    id: str
    usage_summary: str
    indications: list[CrosswalkIndication]


class CrosswalkBatchOutput(BaseModel):
    """PROMPT 2 model output_type -- one CrosswalkResult per input record."""
    model_config = ConfigDict(extra="forbid")
    results: list[CrosswalkResult]


class FdaDrugRecord(BaseModel):
    """PROMPT 2 output (Appendix B) -- one record per dispensable drug, 1:1 with the
    FDA file. The FDA SPL record enriched in place: its native fields pass through
    unchanged (additionalProperties:true) and the enrichment fields below are added.
    Keyed by set_id, never by ingredient_rxcuis."""
    model_config = ConfigDict(extra="allow")
    set_id: str
    record_number: Optional[int] = None
    ingredient_rxcuis: list[str] = Field(default_factory=list)
    ingredient_names: list[str] = Field(default_factory=list)
    resolved_by: Literal["tier1", "tier2", "tier3", "floor"]
    resolution_confidence: Optional[float] = Field(default=None, ge=0.0, le=1.0)
    resolution_confidence_rationale: Optional[str] = None
    indications: list[CrosswalkIndication]
    usage_summary: Optional[str] = None


class _DrugKey(BaseModel):
    """Internal: the fields the funnel and the crosswalk need from one FDA record."""
    model_config = ConfigDict(extra="forbid")
    id: str
    set_id: str
    molecule: str
    openfda_rxcui: list[str] = Field(default_factory=list)
    indication_text: str


# ─────────────────────────────────────────────────────────────────────────────
# Catalog loading (lazy; no file read at import)
# ─────────────────────────────────────────────────────────────────────────────
def load_catalog_from_xlsx(path: str) -> tuple[str, frozenset[str], dict[str, str]]:
    """Read the ICD-10-CM 3/4-character catalog from an xlsx into the cached prefix
    text ("<code><TAB><short_description>", one per line), the membership set, and a
    code->short_description map (used to stamp each crosswalk code's description)."""
    from openpyxl import load_workbook

    absolute = str(path)
    if not os.path.isfile(absolute):
        raise ChatHealthyException(
            mode="config_error",
            component="FDADrugEnrichmentTool",
            message=f"ICD-10-CM catalog not found at {absolute!r}",
        )
    wb = load_workbook(absolute, read_only=True)
    ws = wb.active
    rows = ws.iter_rows(values_only=True)
    header = next(rows, None)
    if not header:
        raise ChatHealthyException(
            mode="config_error",
            component="FDADrugEnrichmentTool",
            message=f"ICD-10-CM catalog {absolute!r} is empty",
        )
    index = {str(h).strip().lower(): i for i, h in enumerate(header) if h is not None}
    for required in ("icd10_code", "level", "short_description"):
        if required not in index:
            raise ChatHealthyException(
                mode="config_error",
                component="FDADrugEnrichmentTool",
                message=(f"ICD-10-CM catalog {absolute!r} missing column {required!r}; "
                         f"found {sorted(index)}"),
            )
    lines: list[str] = []
    codes: set[str] = set()
    descriptions: dict[str, str] = {}
    for row in rows:
        level = row[index["level"]]
        if level not in (3, 4):
            continue
        code = str(row[index["icd10_code"]] or "").strip()
        if not code:
            continue
        desc = str(row[index["short_description"]] or "").strip()
        lines.append(f"{code}\t{desc}")
        codes.add(code)
        descriptions[code] = desc
    return "\n".join(lines), frozenset(codes), descriptions


# ─────────────────────────────────────────────────────────────────────────────
# The tool
# ─────────────────────────────────────────────────────────────────────────────
class FDADrugEnrichmentTool:
    """One claims-blind pydantic-ai agent, two single-concern calls (PROMPT 1 then
    PROMPT 2). Construction performs no model call and no network or database I/O."""

    def __init__(
        self,
        *,
        model: Optional[str] = None,
        catalog: Optional[Any] = None,
        rxnav_base_url: Optional[str] = None,
        http_timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
        max_transient_retries: int = _MAX_TRANSIENT_RETRIES,
    ) -> None:
        # Model resolved from configuration, not pinned in source.
        self._model_id: Optional[str] = model or (os.environ.get(_MODEL_ENV, "").strip() or None)
        # Catalog: a path, a prepared (text, codes) pair, or None (resolve from env lazily).
        self._catalog_source = catalog
        self._catalog_text: Optional[str] = None
        self._catalog_codes: Optional[frozenset[str]] = None
        self._catalog_descriptions: Optional[dict[str, str]] = None
        if isinstance(catalog, tuple) and len(catalog) in (2, 3):
            self._catalog_text, self._catalog_codes = catalog[0], frozenset(catalog[1])
            self._catalog_descriptions = dict(catalog[2]) if len(catalog) == 3 else {}
            self._catalog_source = None
        self._rxnav_base = (rxnav_base_url or os.environ.get(_RXNAV_ENV, "").strip()
                            or _RXNAV_DEFAULT_BASE).rstrip("/")
        self._http_timeout = float(http_timeout_seconds)
        self._max_transient_retries = int(max_transient_retries)
        # Lazily built agents (no network until a call fires).
        self._resolution_agent = None
        self._crosswalk_agent = None
        self._crosswalk_system_prompt: Optional[str] = None
        # Per-call usage of the most recent PROMPT 1 / PROMPT 2 model call. The
        # pydantic output_type is unchanged; usage is surfaced through these
        # accessors so a caller can cost the run (section 8) without the tool
        # altering what it returns. None until the respective method runs.
        self._last_resolution_usage: Optional[dict] = None
        self._last_crosswalk_usage: Optional[dict] = None

    # ── configuration helpers ────────────────────────────────────────────────
    def _require_model_id(self) -> str:
        if not self._model_id:
            raise ChatHealthyException(
                mode="config_error",
                component="FDADrugEnrichmentTool",
                message=(f"{_MODEL_ENV} is not set; the model is resolved from configuration "
                         "and is never pinned in source"),
            )
        if self._model_id not in _MODEL_REGISTRY:
            raise ChatHealthyException(
                mode="config_error",
                component="FDADrugEnrichmentTool",
                message=(f"model {self._model_id!r} is not in the registered call-shape set "
                         f"{sorted(_MODEL_REGISTRY)}"),
            )
        return self._model_id

    @property
    def catalog_codes(self) -> frozenset[str]:
        """The ICD-10-CM catalog membership set (for consumers that score hallucination)."""
        self._ensure_catalog()
        return self._catalog_codes  # type: ignore[return-value]

    def _ensure_catalog(self) -> None:
        if self._catalog_text is not None and self._catalog_codes is not None:
            return
        source = self._catalog_source or (os.environ.get(_CATALOG_ENV, "").strip() or None)
        if not source:
            raise ChatHealthyException(
                mode="config_error",
                component="FDADrugEnrichmentTool",
                message=(f"no ICD-10-CM catalog provided and {_CATALOG_ENV} is not set; "
                         "PROMPT 2's cached prefix is the catalog and cannot be built"),
            )
        self._catalog_text, self._catalog_codes, self._catalog_descriptions = (
            load_catalog_from_xlsx(str(source)))

    def _code_description(self, code: str) -> Optional[str]:
        """The ICD-10-CM short description for a catalog code, or None if unknown."""
        self._ensure_catalog()
        return (self._catalog_descriptions or {}).get(code)

    def _build_crosswalk_system_prompt(self) -> str:
        self._ensure_catalog()
        return _PROMPT_2_INSTRUCTIONS + "\n\n" + _CATALOG_HEADER + "\n" + (self._catalog_text or "")

    # ── model construction (Appendix D; lazy, no network) ────────────────────
    def _build_model(self):
        model_id = self._require_model_id()
        spec = _MODEL_REGISTRY[model_id]
        provider = spec["provider"]
        if provider == "openai":
            from pydantic_ai.models.openai import OpenAIResponsesModel, OpenAIResponsesModelSettings
            from pydantic_ai.providers.openai import OpenAIProvider
            model = OpenAIResponsesModel(
                model_id, provider=OpenAIProvider(api_key=os.environ.get("OPENAI_API_KEY")))
            settings = OpenAIResponsesModelSettings(
                openai_reasoning_effort=spec["effort"], max_tokens=_MODEL_MAX_TOKENS)
            return model, settings
        if provider == "google":
            from pydantic_ai.models.google import GoogleModel, GoogleModelSettings
            from pydantic_ai.providers.google import GoogleProvider
            model = GoogleModel(
                model_id,
                provider=GoogleProvider(
                    api_key=os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")))
            settings = GoogleModelSettings(
                google_thinking_config={"thinking_level": spec["thinking_level"]},
                max_tokens=_MODEL_MAX_TOKENS)
            return model, settings
        if provider == "xai":
            from pydantic_ai.models.xai import XaiModel, XaiModelSettings
            from pydantic_ai.providers.xai import XaiProvider
            model = XaiModel(
                model_id,
                provider=XaiProvider(
                    api_key=os.environ.get("XAI_API_KEY") or os.environ.get("GrokXAPIKey")))
            settings = XaiModelSettings(
                xai_reasoning_effort=spec["effort"], max_tokens=_MODEL_MAX_TOKENS)
            return model, settings
        raise ChatHealthyException(
            mode="config_error",
            component="FDADrugEnrichmentTool",
            message=f"unknown provider {provider!r} for model {model_id!r}",
        )

    def _get_resolution_agent(self):
        if self._resolution_agent is None:
            from pydantic_ai import Agent
            model, settings = self._build_model()
            self._resolution_agent = Agent(
                model,
                deps_type=ResolutionBatchInput,
                output_type=Tier3SelectionOutput,
                system_prompt=_PROMPT_1_SYSTEM,
                model_settings=settings,
                retries=2,
            )
        return self._resolution_agent

    def _get_crosswalk_agent(self):
        if self._crosswalk_agent is None:
            from pydantic_ai import Agent
            model, settings = self._build_model()
            self._crosswalk_system_prompt = self._build_crosswalk_system_prompt()
            self._crosswalk_agent = Agent(
                model,
                deps_type=CrosswalkBatchInput,
                output_type=CrosswalkBatchOutput,
                system_prompt=self._crosswalk_system_prompt,
                model_settings=settings,
                retries=2,
            )
        return self._crosswalk_agent

    # ── RxNav REST client (deterministic funnel + tier-3 candidate generation) ─
    def _get_json(self, client: httpx.Client, path: str,
                  params: Optional[dict] = None) -> Optional[dict]:
        """GET {base}{path}; return the parsed body, or None for a 404/negative.
        Transient failures (429/5xx/network/timeout) retry with backoff; an
        exhausted transient failure raises. Deterministic 4xx (other than 429)
        resolve to None -- a normal 'not found' from RxNorm."""
        url = self._rxnav_base + path
        attempt, backoff = 0, 2.0
        while True:
            try:
                response = client.get(url, params=params)
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt >= self._max_transient_retries:
                    raise ChatHealthyException(
                        mode="rxnav_unavailable",
                        component="FDADrugEnrichmentTool",
                        message=f"RxNav request to {url} failed after {attempt} retries",
                        exception=exc,
                        url=url,
                    ) from exc
                time.sleep(backoff)
                backoff *= 2
                attempt += 1
                continue
            status = response.status_code
            if status == 404:
                return None
            if status in _TRANSIENT_STATUS:
                if attempt >= self._max_transient_retries:
                    raise ChatHealthyException(
                        mode="rxnav_unavailable",
                        component="FDADrugEnrichmentTool",
                        message=f"RxNav request to {url} returned {status} after {attempt} retries",
                        url=url,
                        status=status,
                    )
                time.sleep(backoff)
                backoff *= 2
                attempt += 1
                continue
            if status >= 400:
                return None
            return response.json()

    def _concept_tty_name(self, client: httpx.Client, rxcui: str) -> tuple[Optional[str], Optional[str]]:
        body = self._get_json(client, f"/rxcui/{rxcui}/properties.json")
        props = (body or {}).get("properties") or {}
        tty = str(props.get("tty") or "").strip() or None
        name = str(props.get("name") or "").strip() or None
        return tty, name

    def _ingredients_of(self, client: httpx.Client, rxcui: str) -> list[tuple[str, str]]:
        """Return [(ingredient_rxcui, ingredient_name), ...] of tty=IN concepts
        related to rxcui. If rxcui is itself an ingredient, it is its own."""
        tty, name = self._concept_tty_name(client, rxcui)
        if tty == "IN":
            return [(rxcui, name or "")]
        body = self._get_json(client, f"/rxcui/{rxcui}/related.json", {"tty": "IN"})
        found: list[tuple[str, str]] = []
        groups = (((body or {}).get("relatedGroup") or {}).get("conceptGroup") or [])
        for group in groups:
            for concept in group.get("conceptProperties") or []:
                cid = str(concept.get("rxcui") or "").strip()
                if cid:
                    found.append((cid, str(concept.get("name") or "").strip()))
        return found

    def _remapped_rxcuis(self, client: httpx.Client, rxcui: str) -> list[str]:
        """Normalized step: RxNorm status/remap of a non-current rxcui."""
        body = self._get_json(client, f"/rxcui/{rxcui}/status.json")
        status_group = (body or {}).get("rxcuiStatus") or {}
        remapped: list[str] = []
        min_group = status_group.get("minConceptGroup") or {}
        for concept in min_group.get("minConcept") or []:
            cid = str(concept.get("rxcui") or "").strip()
            if cid and cid != rxcui:
                remapped.append(cid)
        return remapped

    def _approximate_candidates(self, client: httpx.Client, term: str) -> list[CandidateConcept]:
        body = self._get_json(
            client, "/approximateTerm.json",
            {"term": term, "maxEntries": str(_APPROX_MAX_CANDIDATES)})
        candidates_raw = (((body or {}).get("approximateGroup") or {}).get("candidate") or [])
        seen: set[str] = set()
        candidates: list[CandidateConcept] = []
        for entry in candidates_raw:
            rxcui = str(entry.get("rxcui") or "").strip()
            if not rxcui or rxcui in seen:
                continue
            seen.add(rxcui)
            tty, name = self._concept_tty_name(client, rxcui)
            ingredients = self._ingredients_of(client, rxcui)
            candidates.append(CandidateConcept(
                rxcui=rxcui, name=name or "", tty=tty or "",
                ingredient_rxcuis=[ing[0] for ing in ingredients],
                ingredient_names=[ing[1] for ing in ingredients]))
        return candidates

    def _deterministic_resolve(self, client: httpx.Client,
                               key: _DrugKey) -> Optional[RxcuiResolutionItem]:
        """tier1 (exact) then tier2 (normalized), both off the label's own
        openfda.rxcui, with no model. Any tty=IN ingredient(s) found resolve
        deterministically -- a single-ingredient drug yields a one-element set, a
        combination its full set; anything unresolved falls to tier3."""
        # tier1 -- direct relation off each of the label's rxcuis.
        exact: dict[str, str] = {}
        exact_names: dict[str, str] = {}
        exact_source: Optional[str] = None
        for product_rxcui in key.openfda_rxcui:
            product_rxcui = str(product_rxcui).strip()
            if not product_rxcui:
                continue
            for ing_rxcui, ing_name in self._ingredients_of(client, product_rxcui):
                exact[ing_rxcui] = product_rxcui
                exact_names[ing_rxcui] = ing_name
                exact_source = exact_source or product_rxcui
        if exact:
            ordered = sorted(exact)
            return RxcuiResolutionItem(
                id=key.id, rxcui=exact_source, ingredient_rxcuis=ordered,
                ingredient_names=[exact_names.get(r, "") for r in ordered],
                resolved_by="tier1", confidence=1.0, confidence_rationale="Deterministic")
        # tier2 -- normalized via status/remap of each label rxcui.
        normalized: dict[str, str] = {}
        normalized_names: dict[str, str] = {}
        normalized_source: Optional[str] = None
        for product_rxcui in key.openfda_rxcui:
            product_rxcui = str(product_rxcui).strip()
            if not product_rxcui:
                continue
            for remapped in self._remapped_rxcuis(client, product_rxcui):
                for ing_rxcui, ing_name in self._ingredients_of(client, remapped):
                    normalized[ing_rxcui] = remapped
                    normalized_names[ing_rxcui] = ing_name
                    normalized_source = normalized_source or remapped
        if normalized:
            ordered = sorted(normalized)
            return RxcuiResolutionItem(
                id=key.id, rxcui=normalized_source, ingredient_rxcuis=ordered,
                ingredient_names=[normalized_names.get(r, "") for r in ordered],
                resolved_by="tier2", confidence=1.0, confidence_rationale="Deterministic")
        return None

    # ── PROMPT 1 -- the RXCUI resolution funnel ──────────────────────────────
    def resolve_rxcuis(self, drugs: Iterable[_DrugKey]) -> RxcuiResolutionOutput:
        """Run the funnel over a batch of drugs and return RxcuiResolutionOutput
        (Appendix B). tier1/tier2 are deterministic RxNav lookups; tier3 is one
        bounded model call over the residue only; the floor is reported by reason.
        FIRES RxNav calls, and a model call only if a residue exists."""
        keys = list(drugs)
        # No model call unless a residue exists; record that so the caller can
        # cost a fully-deterministic batch as zero model tokens (section 8).
        self._last_resolution_usage = {
            "input_tokens": 0, "output_tokens": 0, "cached_input_tokens": 0,
            "transient_retries": 0, "model_call_fired": False}
        resolved: dict[str, RxcuiResolutionItem] = {}
        residue: list[UnresolvedDrug] = []
        with httpx.Client(timeout=self._http_timeout) as client:
            for key in keys:
                item = self._deterministic_resolve(client, key)
                if item is not None:
                    resolved[key.id] = item
                    continue
                candidates = self._approximate_candidates(client, key.molecule)
                residue.append(UnresolvedDrug(id=key.id, name=key.molecule, candidates=candidates))
        if residue:
            selections, meta = self._run_tier3(residue)
            self._last_resolution_usage = {**meta, "model_call_fired": True}
            # Keep the whole candidate concept per id (not just its rxcui) so the
            # join key is RE-DERIVED from the CHOSEN candidate's own tty=IN
            # ingredient, never taken from the model's returned ingredient_rxcui.
            candidates_by_id = {d.id: {c.rxcui: c for c in d.candidates} for d in residue}
            for selection in selections.results:
                allowed = candidates_by_id.get(selection.id, {})
                chosen_candidate = (allowed.get(selection.rxcui)
                                    if selection.rxcui is not None else None)
                if chosen_candidate is not None:
                    # Candidate-constrained join key: the selected concept's tty=IN
                    # ingredient(s), as RxNav returned them -- all of them for a
                    # combination, re-derived from the chosen candidate and never the
                    # model's free text. An empty array (concept with no IN ingredient)
                    # is a floor on the join, never an invented identifier.
                    resolved[selection.id] = RxcuiResolutionItem(
                        id=selection.id, rxcui=chosen_candidate.rxcui,
                        ingredient_rxcuis=list(chosen_candidate.ingredient_rxcuis),
                        ingredient_names=list(chosen_candidate.ingredient_names),
                        resolved_by="tier3", confidence=selection.confidence,
                        confidence_rationale=selection.confidence_rationale)
                else:
                    resolved[selection.id] = RxcuiResolutionItem(
                        id=selection.id, rxcui=None, ingredient_rxcuis=[],
                        ingredient_names=[],
                        resolved_by="floor", confidence=0.0,
                        confidence_rationale=selection.confidence_rationale)
        # Any residue name the model did not return a result for is floored by reason.
        for drug in residue:
            resolved.setdefault(drug.id, RxcuiResolutionItem(
                id=drug.id, rxcui=None, ingredient_rxcuis=[], ingredient_names=[],
                resolved_by="floor", confidence=0.0,
                confidence_rationale=_RESOLUTION_DROP_RATIONALE))
        ordered = [resolved[key.id] for key in keys]
        return RxcuiResolutionOutput(results=ordered)

    def _run_tier3(self, residue: list[UnresolvedDrug]) -> tuple[Tier3SelectionOutput, dict]:
        agent = self._get_resolution_agent()
        deps = ResolutionBatchInput(drugs=residue)
        payload = [
            {
                "id": drug.id,
                "name": drug.name,
                "candidates": [c.model_dump(exclude_none=True) for c in drug.candidates],
            }
            for drug in residue
        ]
        user_message = ("Unresolved drug names and their RxNorm approximate-match candidates:\n"
                        + json.dumps(payload, ensure_ascii=False, indent=1))
        output, meta = self._run_agent(agent, user_message, deps, call_site="prompt1_tier3")
        return output, meta

    # ── per-call usage accessors (Appendix D / section 8 costing) ─────────────
    def last_resolution_usage(self) -> Optional[dict]:
        """Token/retry usage of the most recent resolve_rxcuis() call (PROMPT 1):
        input_tokens, output_tokens, cached_input_tokens, transient_retries, and
        model_call_fired (False when the batch resolved entirely deterministically).
        None until resolve_rxcuis() has run. Does not alter the pydantic output."""
        return self._last_resolution_usage

    def last_crosswalk_usage(self) -> Optional[dict]:
        """Token/retry usage of the most recent crosswalk() call (PROMPT 2). None
        until crosswalk() has run. Does not alter the pydantic output."""
        return self._last_crosswalk_usage

    # ── PROMPT 2 -- the crosswalk ────────────────────────────────────────────
    def crosswalk(self, batch: CrosswalkBatchInput) -> CrosswalkBatchOutput:
        """Run the indication -> ICD-10-CM crosswalk over one batch (<=50 records)
        and return CrosswalkBatchOutput. FIRES a model call."""
        agent = self._get_crosswalk_agent()
        output, meta = self._run_agent(
            agent, self._crosswalk_user_message(batch), batch, call_site="prompt2_crosswalk")
        self._last_crosswalk_usage = meta
        return output

    @staticmethod
    def _crosswalk_user_message(batch: CrosswalkBatchInput) -> str:
        drugs: list[dict] = []
        for drug in batch.drugs:
            record: dict[str, Any] = {
                "id": drug.id,
                "set_id": drug.set_id,
                "molecule": drug.molecule,
                "indication_text": drug.indication_text,
            }
            if drug.rxcui:  # inserted only above the 0.75 threshold; absent otherwise
                record["rxcui"] = drug.rxcui
            drugs.append(record)
        return "Drugs:\n" + json.dumps({"drugs": drugs}, ensure_ascii=False, indent=1)

    # ── shared model-call runner (transient retry; never fabricates) ─────────
    def _run_agent(self, agent, user_message: str, deps, *, call_site: str):
        attempt, backoff = 0, 2.0
        while True:
            try:
                result = agent.run_sync(user_message, deps=deps)
                return result.output, self._extract_usage(result, attempt)
            except Exception as exc:  # noqa: BLE001  record, retry transient, never fabricate
                text = str(exc).lower()
                transient = any(token in text for token in (
                    "429", "rate", "timeout", "timed out", "500", "502", "503", "504",
                    "529", "overloaded", "connection"))
                if not transient or attempt >= self._max_transient_retries:
                    raise ChatHealthyException(
                        mode="llm_unavailable",
                        component="FDADrugEnrichmentTool",
                        message=(f"{call_site} model call failed after {attempt} transient "
                                 f"retries: {type(exc).__name__}"),
                        exception=exc,
                        call_site=call_site,
                        model=self._model_id,
                        attempts=attempt,
                    ) from exc
                time.sleep(backoff)
                backoff *= 2
                attempt += 1

    @staticmethod
    def _extract_usage(result, attempt: int) -> dict:
        """Best-effort token capture; a slip here must never discard a paid result."""
        usage: dict[str, Any] = {"transient_retries": attempt}
        try:
            value = result.usage
            usage["input_tokens"] = getattr(value, "input_tokens", None)
            usage["output_tokens"] = getattr(value, "output_tokens", None)
            usage["cached_input_tokens"] = getattr(value, "cache_read_tokens", None)
            usage["total_tokens"] = getattr(value, "total_tokens", None)
        except Exception as exc:  # noqa: BLE001
            usage["usage_error"] = repr(exc)[:200]
        return usage

    # ── end-to-end enrichment (PROMPT 1 then PROMPT 2, assembled per Appendix B)
    def enrich(
        self,
        fda_records: list[dict],
        *,
        record_number_start: int = 1,
        sink: Optional[Callable[[FdaDrugRecord], None]] = None,
        on_batch_error: Optional[Callable[[list, Exception], None]] = None,
    ) -> list[FdaDrugRecord]:
        """For each FDA SPL record: resolve its RXCUI (PROMPT 1), insert it into the
        crosswalk input only above the 0.75 threshold, crosswalk (PROMPT 2), and
        assemble the FdaDrugRecord (the FDA record enriched in
        place). Processes in batches of the production batch size. Each assembled
        record is written through the pluggable sink when one is supplied; the raw
        PROMPT 1 and PROMPT 2 outputs are returned to the caller via resolve_rxcuis()
        and crosswalk() for persist-first scratch. FIRES model and RxNav calls."""
        records: list[FdaDrugRecord] = []
        record_number = record_number_start
        skipped_records = 0
        for start in range(0, len(fda_records), _CROSSWALK_BATCH_SIZE):
            chunk = fda_records[start:start + _CROSSWALK_BATCH_SIZE]
            keys = [self._extract_key(record, start + offset) for offset, record in enumerate(chunk)]
            try:
                resolution = self.resolve_rxcuis(keys)
                resolution_by_id = {item.id: item for item in resolution.results}

                drug_inputs: list[DrugInput] = []
                for key in keys:
                    item = resolution_by_id[key.id]
                    insert = bool(item.ingredient_rxcuis) and item.confidence > _INSERT_THRESHOLD
                    drug_inputs.append(DrugInput(
                        id=key.id, set_id=key.set_id, molecule=key.molecule,
                        rxcui=list(item.ingredient_rxcuis) if insert else [],
                        indication_text=key.indication_text))
                crosswalk_output = self.crosswalk(CrosswalkBatchInput(drugs=drug_inputs))
            except ChatHealthyException as exc:
                # A model call for this batch threw (crosswalk returned schema-
                # invalid output, an incomplete tool call, or the resolver's model
                # failed). Recover: record the error and skip this batch, so one
                # bad model response does not fail the whole ~2000-record partition.
                # The catcher logs; it does not re-raise.
                _log.LogPipeline(
                    "ERROR", "FDADrugEnrichmentTool.enrich: batch of %d record(s) "
                    "skipped after model failure: %s", len(chunk), str(exc)[:200])
                if on_batch_error is not None:
                    on_batch_error(keys, exc)
                skipped_records += len(chunk)
                record_number += len(chunk)
                continue
            crosswalk_by_id = {result.id: result for result in crosswalk_output.results}

            for key, fda_record in zip(keys, chunk):
                item = resolution_by_id[key.id]
                crosswalk_result = crosswalk_by_id.get(key.id)
                record = self._assemble(fda_record, key, item, crosswalk_result, record_number)
                if sink is not None:
                    sink(record)
                records.append(record)
                record_number += 1
        _log.LogPipeline(
            "INFO", "FDADrugEnrichmentTool.enrich: records=%d skipped=%d model=%s",
            len(records), skipped_records, self._model_id)
        return records

    @staticmethod
    def _extract_key(fda_record: dict, position: int) -> _DrugKey:
        set_id = str(fda_record.get("set_id") or "").strip()
        molecule = str(fda_record.get("molecule") or "").strip()
        indication_text = str(fda_record.get("indication_text") or "").strip()
        missing = [name for name, value in
                   (("set_id", set_id), ("molecule", molecule), ("indication_text", indication_text))
                   if not value]
        if missing:
            raise ChatHealthyException(
                mode="value_error",
                component="FDADrugEnrichmentTool",
                message=(f"FDA record at position {position} is missing required field(s): "
                         f"{', '.join(missing)}"),
            )
        openfda = fda_record.get("openfda") or {}
        openfda_rxcui = [str(v).strip() for v in (openfda.get("rxcui") or []) if str(v).strip()]
        record_id = str(fda_record.get("id") or position)
        return _DrugKey(id=record_id, set_id=set_id, molecule=molecule,
                        openfda_rxcui=openfda_rxcui, indication_text=indication_text)

    def _assemble(
        self,
        fda_record: dict,
        key: _DrugKey,
        resolution: RxcuiResolutionItem,
        crosswalk_result: Optional[CrosswalkResult],
        record_number: int,
    ) -> FdaDrugRecord:
        """The FDA SPL record enriched in place: native fields pass through unchanged,
        the resolution fields are carried from PROMPT 1, and the crosswalk's
        indications and usage_summary are added (Appendix B). Each crosswalk code is
        stamped with its ICD-10-CM short description from the loaded catalog."""
        data: dict[str, Any] = dict(fda_record)
        data["set_id"] = key.set_id
        data["record_number"] = record_number
        data["ingredient_rxcuis"] = resolution.ingredient_rxcuis
        data["ingredient_names"] = resolution.ingredient_names
        data["resolved_by"] = resolution.resolved_by
        data["resolution_confidence"] = resolution.confidence
        data["resolution_confidence_rationale"] = resolution.confidence_rationale
        indications: list[dict] = []
        if crosswalk_result is not None:
            for indication in crosswalk_result.indications:
                for code in indication.codes:
                    code.description = self._code_description(code.icd10_code)
                indications.append(indication.model_dump())
        data["indications"] = indications
        data["usage_summary"] = crosswalk_result.usage_summary if crosswalk_result is not None else None
        return FdaDrugRecord(**data)
