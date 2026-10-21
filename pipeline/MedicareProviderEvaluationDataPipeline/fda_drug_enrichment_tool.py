"""FDA drug enrichment tool (EPIC-010-F-007, LLD v83 III.12) -- the ONE
sanctioned external model call of the Medicare build: the indication ->
ICD-10-CM crosswalk (PROMPT 2). One claims-blind pydantic-ai agent; typed
pydantic input and output; the model resolved from configuration.

Molecule identity is NOT resolved here: it is the proprietary molecule_id
surrogate (III.2 decision #2) plus a LOCAL join against the staged RxNorm
vocabulary at finalize_fda_molecules (III.9/III.10). The former runtime
RxNav API resolution (PROMPT 1) was an off-design holdover removed
2026-10-20 -- it throttled at the service's per-IP limit and destroyed
coverage in two runs (operator-confirmed bug).

LLM THROTTLING IS BACKPRESSURE (hard-fail table ruling, 2026-10-20): an
HTTP 429 / rate-limit response waits and retries indefinitely -- bounded
only by the step timeout (H9) -- and NEVER drops the batch. Other
transient failures retry up to the configured bound. The batch size and
retry knobs are REQUIRED constructor arguments supplied from the per-env
durable configuration (H7: no defaults in code).
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration (resolved from the environment; never pinned in source)
# ─────────────────────────────────────────────────────────────────────────────
_MODEL_ENV = "CH_FDA_ENRICHMENT_MODEL"          # e.g. "gpt-5.6-luna"
_CATALOG_ENV = "CH_ICD10_CATALOG_PATH"          # path to the ICD-10-CM catalog xlsx

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
    unchanged (additionalProperties:true) and the crosswalk enrichment fields below
    are added. Keyed by set_id. Molecule identity (rxcui/unii) is NOT carried here:
    it is unioned from the label's openfda fields and the staged RxNorm vocabulary
    at finalize_fda_molecules (LLD v83 III.9/III.10)."""
    model_config = ConfigDict(extra="allow")
    set_id: str
    record_number: Optional[int] = None
    indications: list[CrosswalkIndication]
    usage_summary: Optional[str] = None


class _DrugKey(BaseModel):
    """Internal: the fields the funnel and the crosswalk need from one FDA record."""
    model_config = ConfigDict(extra="forbid")
    id: str
    set_id: str
    molecule: str
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
        batch_size: int,
        max_transient_retries: int,
        backoff_base_s: float,
        model: Optional[str] = None,
        catalog: Optional[Any] = None,
    ) -> None:
        # The tunable levers are REQUIRED and come from the per-env durable
        # configuration (fda.crosswalk_batch_size, fda.llm_retry) -- H7: no
        # defaults in code.
        self._batch_size = int(batch_size)
        self._backoff_base_s = float(backoff_base_s)
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
        self._max_transient_retries = int(max_transient_retries)
        # Lazily built agent (no network until a call fires).
        self._crosswalk_agent = None
        self._crosswalk_system_prompt: Optional[str] = None
        # Per-call usage of the most recent crosswalk model call, surfaced so a
        # caller can cost the run without the tool altering what it returns.
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

    # ── per-call usage accessors (Appendix D / section 8 costing) ─────────────
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
        """Run one model call. THROTTLING IS BACKPRESSURE (LLD v83 III.15,
        operator-ruled 2026-10-20): a 429/rate-limit response waits and retries
        INDEFINITELY -- bounded only by the step timeout (H9) -- and never
        surfaces as a failure, so a throttled batch is never dropped. Other
        transient failures retry up to the configured bound; deterministic
        failures raise immediately."""
        attempt, backoff = 0, self._backoff_base_s
        throttle_waits = 0
        wall_t0 = time.perf_counter()
        while True:
            try:
                call_t0 = time.perf_counter()
                result = agent.run_sync(user_message, deps=deps)
                usage = self._extract_usage(result, attempt)
                usage["throttle_waits"] = throttle_waits
                # Instrumentation (operator-ordered 2026-10-21): the duration of
                # THIS model call, and the wall time including every retry/wait.
                usage["model_call_s"] = round(time.perf_counter() - call_t0, 3)
                usage["wall_s"] = round(time.perf_counter() - wall_t0, 3)
                _log.LogLlmCall(
                    call_site=call_site, model=str(self._model_id), outcome="ok",
                    model_call_s=usage["model_call_s"], wall_s=usage["wall_s"],
                    input_tokens=usage.get("input_tokens"),
                    output_tokens=usage.get("output_tokens"),
                    throttle_waits=throttle_waits, transient_retries=attempt)
                return result.output, usage
            except Exception as exc:  # noqa: BLE001  record, retry transient, never fabricate
                text = str(exc).lower()
                throttled = ("429" in text or "rate limit" in text
                             or "rate_limit" in text or "too many requests" in text)
                if throttled:
                    throttle_waits += 1
                    wait = min(backoff, 60.0)
                    _log.LogPipeline(
                        "WARNING",
                        "%s throttled by the model provider (wait %d, %.0fs); "
                        "backpressure, not failure", call_site, throttle_waits, wait)
                    time.sleep(wait)
                    backoff = min(backoff * 2, 60.0)
                    continue
                transient = any(token in text for token in (
                    "timeout", "timed out", "500", "502", "503", "504",
                    "529", "overloaded", "connection"))
                if not transient or attempt >= self._max_transient_retries:
                    _log.LogLlmCall(
                        call_site=call_site, model=str(self._model_id),
                        outcome=f"failed:{type(exc).__name__}",
                        wall_s=round(time.perf_counter() - wall_t0, 3),
                        throttle_waits=throttle_waits, transient_retries=attempt)
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
                backoff = min(backoff * 2, 60.0)
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

    # ── end-to-end enrichment (the crosswalk, assembled per Appendix B) ──────
    def enrich(
        self,
        fda_records: list[dict],
        *,
        record_number_start: int = 1,
        sink: Optional[Callable[[FdaDrugRecord], None]] = None,
        on_batch_error: Optional[Callable[[list, Exception], None]] = None,
        on_batch_usage: Optional[Callable[[dict], None]] = None,
    ) -> list[FdaDrugRecord]:
        """For each FDA SPL record: crosswalk its indication text to ICD-10-CM
        (PROMPT 2 -- the one sanctioned model call) and assemble the
        FdaDrugRecord (the FDA record enriched in place). Processes in batches
        of the configured batch size. Each assembled record is written through
        the pluggable sink when one is supplied. FIRES model calls only --
        nothing else leaves the box."""
        records: list[FdaDrugRecord] = []
        record_number = record_number_start
        skipped_records = 0
        for start in range(0, len(fda_records), self._batch_size):
            chunk = fda_records[start:start + self._batch_size]
            keys = [self._extract_key(record, start + offset) for offset, record in enumerate(chunk)]
            try:
                drug_inputs = [
                    DrugInput(id=key.id, set_id=key.set_id, molecule=key.molecule,
                              indication_text=key.indication_text)
                    for key in keys
                ]
                crosswalk_output = self.crosswalk(CrosswalkBatchInput(drugs=drug_inputs))
                if on_batch_usage is not None:
                    on_batch_usage({"batch_size": len(chunk), "outcome": "ok",
                                    "model": str(self._model_id),
                                    **(self._last_crosswalk_usage or {})})
            except ChatHealthyException as exc:
                # A NON-THROTTLE model failure for this batch (throttles never
                # reach here -- _run_agent absorbs them as backpressure).
                # Recover: record the error and skip this batch, so one bad
                # model response does not fail the whole partition.
                _log.LogPipeline(
                    "ERROR", "FDADrugEnrichmentTool.enrich: batch of %d record(s) "
                    "skipped after model failure: %s", len(chunk), str(exc)[:200])
                if on_batch_usage is not None:
                    on_batch_usage({"batch_size": len(chunk),
                                    "outcome": f"failed:{exc.mode}",
                                    "model": str(self._model_id)})
                if on_batch_error is not None:
                    on_batch_error(keys, exc)
                skipped_records += len(chunk)
                record_number += len(chunk)
                continue
            crosswalk_by_id = {result.id: result for result in crosswalk_output.results}

            for key, fda_record in zip(keys, chunk):
                crosswalk_result = crosswalk_by_id.get(key.id)
                record = self._assemble(fda_record, key, crosswalk_result, record_number)
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
        record_id = str(fda_record.get("id") or position)
        return _DrugKey(id=record_id, set_id=set_id, molecule=molecule,
                        indication_text=indication_text)

    def _assemble(
        self,
        fda_record: dict,
        key: _DrugKey,
        crosswalk_result: Optional[CrosswalkResult],
        record_number: int,
    ) -> FdaDrugRecord:
        """The FDA SPL record enriched in place: native fields pass through
        unchanged and the crosswalk's indications and usage_summary are added
        (Appendix B). Each crosswalk code is stamped with its ICD-10-CM short
        description from the loaded catalog."""
        data: dict[str, Any] = dict(fda_record)
        data["set_id"] = key.set_id
        data["record_number"] = record_number
        indications: list[dict] = []
        if crosswalk_result is not None:
            for indication in crosswalk_result.indications:
                for code in indication.codes:
                    code.description = self._code_description(code.icd10_code)
                indications.append(indication.model_dump())
        data["indications"] = indications
        data["usage_summary"] = crosswalk_result.usage_summary if crosswalk_result is not None else None
        return FdaDrugRecord(**data)
