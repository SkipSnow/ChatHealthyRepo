# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""NPPES Other Provider Identifier phrase pipeline — harvest, classify, apply.

The classify step MUST run every phrase through an LLM via a PydanticAI
Agent with structured input AND structured output. No hand-curated
substring tables, no fallback rules, no free-form prompt-output
parsing.

Staging is per-run and scoped to (phrase, state). The staging table
`PublicStaging.OtherIdentifierPhrases` is rebuilt at the start of
every harvest — prior runs' rows are dropped. The business key on
each staging document is (type_code, issuer_text, business_state):

  - two providers in VT with the same issuer_text share one staging
    row for VT;
  - the same issuer_text seen in a DE provider gets its own DE
    staging row and is classified independently (state context
    matters — "MEDICAID" in VT means VT Medicaid; "STATE LICENSE"
    in DE means DE medical license);
  - dropping at harvest start scopes the LLM classification work to
    only what the current run's data warrants, and keeps LLM context
    windows small (one state's phrases at a time).

Three stages, each callable independently:

  harvest_other_identifier_phrases(config, mongo)
      Serial. Deletes prior-run staging rows. Scans the target
      Provider collection; upserts one row per unique
      (type_code, issuer_text, business_state) with occurrence counts
      and sample identifiers.

  classify_other_identifier_phrases(config, mongo)
      Per-state fanout. Reads all rows for the partition's state where
      classification is null. Batch-calls the PydanticAI Agent with a
      BatchClassificationInput; persists BatchClassificationOutput back
      to each row.

  apply_other_identifier_classifications(config, mongo)
      Per-state fanout. Walks provider records for the partition's
      state; for each other_identifiers[] entry, looks up the
      (phrase, state) row and routes:
        - INSURANCE  → insurance[] with canonical_payer_name +
                       payer_type + payer_id + state + source.
        - LICENSE    → licenses[] with license_type + number + state
                       + source; existing dedupe_licenses merges with
                       NPPES-native licenses.
        - PROVIDER_ID / FACILITY_ID → retained in other_identifiers[]
                       tagged with classification.
        - UNKNOWN    → retained in other_identifiers[] with
                       classification_pending: True (should not happen
                       normally — the LLM must return a classification
                       for every phrase in the input list).
"""

from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import hashlib
import os
import time
from datetime import datetime, timezone
from typing import Any


_log = ChatHealthyLoggingService()


STAGING_DB = "PublicStaging"
STAGING_COLL = "OtherIdentifierPhrases"

_LLM_MODEL_ID = "google-gla:gemini-2.5-flash"
_LLM_MODEL_NAME_FOR_AUDIT = "gemini-2.5-flash"
_LLM_TIMEOUT_S = 300


# ── phrase-key computation ─────────────────────────────────────────────────

def _phrase_state_id(type_code: str, issuer_text: str, state: str) -> str:
    """Stable staging-table _id per unique (type_code, issuer_text, state)."""
    tc = (type_code or "").strip()
    it = (issuer_text or "").strip().upper()
    st = (state or "").strip().upper()
    return hashlib.sha256(f"{st}|{tc}|{it}".encode("utf-8")).hexdigest()


def _business_state_of(doc: dict) -> str | None:
    for a in (doc.get("addresses") or []):
        if isinstance(a, dict) and a.get("address_type") == "business":
            st = (a.get("state") or "").strip().upper()
            if st:
                return st
    return None


def _resolve_provider_collection(config: dict, mongo):
    provider_collection = config["provider_collection"]
    db_name, coll_name = provider_collection.split(".", 1)
    return mongo[db_name][coll_name]


def _resolve_staging_collection(mongo):
    return mongo[STAGING_DB][STAGING_COLL]


# ── Stage 1: harvest ───────────────────────────────────────────────────────

def harvest_other_identifier_phrases(config: dict, *, mongo, blob=None) -> dict:
    """Rebuild the staging table for this run's state scope. Deletes
    prior rows FILTERED to the current state scope (never touches
    other states' rows), then upserts one row per (type_code,
    issuer_text, state) for the providers in scope.

    Config keys used:
      - run_id
      - provider_collection: "<db>.<coll>" on pipeline cluster
      - states: list[str] — state scope for this run (uppercased).
                Empty/absent = whole collection (ALL states).
    """
    run_id = config["run_id"]
    provider_coll = _resolve_provider_collection(config, mongo)
    staging_coll = _resolve_staging_collection(mongo)
    scoped_states = [
        (s or "").upper().strip()
        for s in (config.get("states") or [])
        if s
    ]

    if scoped_states:
        prior_deleted = staging_coll.delete_many(
            {"state": {"$in": scoped_states}}
        ).deleted_count
    else:
        prior_deleted = staging_coll.delete_many({}).deleted_count

    provider_query: dict[str, Any] = {
        "other_identifiers": {"$exists": True, "$ne": []},
    }
    if scoped_states:
        provider_query["addresses"] = {"$elemMatch": {
            "address_type": "business",
            "state": {"$in": scoped_states},
        }}

    accumulator: dict[str, dict[str, Any]] = {}
    scanned = 0
    entries_seen = 0

    cursor = provider_coll.find(
        provider_query,
        {"other_identifiers": 1, "addresses": 1, "_id": 0},
        no_cursor_timeout=True,
    )
    scoped_set = set(scoped_states)
    try:
        for doc in cursor:
            scanned += 1
            state = _business_state_of(doc)
            if not state:
                continue
            if scoped_set and state not in scoped_set:
                continue
            for entry in doc.get("other_identifiers") or []:
                if not isinstance(entry, dict):
                    continue
                tc = (entry.get("type_code") or "").strip()
                it = (entry.get("issuer") or "").strip().upper()
                idf = (entry.get("identifier") or "").strip()
                pid = _phrase_state_id(tc, it, state)
                slot = accumulator.setdefault(pid, {
                    "_id": pid,
                    "type_code": tc,
                    "issuer_text": it,
                    "state": state,
                    "samples": [],
                    "count": 0,
                })
                slot["count"] += 1
                if idf and len(slot["samples"]) < 3:
                    slot["samples"].append(idf)
                entries_seen += 1
    finally:
        cursor.close()

    now_iso = datetime.now(timezone.utc).isoformat()
    upserted = 0
    for pid, slot in accumulator.items():
        staging_coll.update_one(
            {"_id": pid},
            {
                "$set": {
                    "type_code": slot["type_code"],
                    "issuer_text": slot["issuer_text"],
                    "state": slot["state"],
                    "sample_identifiers": slot["samples"],
                    "occurrence_count": slot["count"],
                    "run_id": run_id,
                    "harvested_at": now_iso,
                    "classification": None,
                    "payer_type": None,
                    "canonical_payer_name": None,
                    "license_type": None,
                    "reasoning": None,
                    "classified_at": None,
                    "classified_by": None,
                },
            },
            upsert=True,
        )
        upserted += 1

    _log.info(
        "harvest_other_identifier_phrases: scanned=%d entries=%d "
        "unique_phrase_state=%d prior_deleted=%d",
        scanned, entries_seen, upserted, prior_deleted,
    )
    return {
        "providers_scanned": scanned,
        "entries_seen": entries_seen,
        "unique_phrase_state_rows": upserted,
        "prior_deleted": prior_deleted,
    }


# ── Stage 2: classify (PydanticAI Agent, structured I/O) ──────────────────

def _build_pydantic_ai_schemas():
    """Return (PhraseIn, BatchClassificationInput, BatchClassificationOutput).
    Import cost is paid inside the classify worker only."""
    from pydantic import BaseModel, Field
    from typing import List, Optional, Literal

    class PhraseIn(BaseModel):
        type_code: str = Field(
            description="NPPES Other Provider Identifier Type Code "
                        "(\"01\"=OTHER, \"05\"=MEDICAID, or empty).")
        issuer_text: str = Field(
            description="Verbatim upper-cased issuer text from NPPES.")
        sample_identifiers: List[str] = Field(
            description="Up to 3 real identifier samples for format hints.")

    class BatchClassificationInput(BaseModel):
        state: str = Field(description="US state code the batch is scoped to.")
        phrases: List[PhraseIn]

    class PhraseOut(BaseModel):
        type_code: str
        issuer_text: str
        classification: Literal[
            "INSURANCE", "LICENSE", "PROVIDER_ID", "FACILITY_ID", "UNKNOWN"
        ] = Field(
            description="Category. INSURANCE=payer enrollment ID. "
                        "LICENSE=state or professional license number. "
                        "PROVIDER_ID=provider-directory ID (NPI, TAX ID, "
                        "CAQH, NCPDP, NABP, UPIN). FACILITY_ID=facility "
                        "ID (CLIA, DME). UNKNOWN=cannot classify.")
        payer_type: Optional[Literal["MEDICARE", "MEDICAID", "COMMERCIAL"]] = Field(
            default=None,
            description="Set only when classification=INSURANCE. MEDICARE "
                        "covers original Medicare, Railroad Medicare, "
                        "Medicare Administrative Contractors (Palmetto GBA), "
                        "and PTAN. MEDICAID covers any state Medicaid. "
                        "COMMERCIAL covers everything else, including "
                        "TRICARE/CHAMPUS.")
        canonical_payer_name: Optional[str] = Field(
            default=None,
            description="Set only when classification=INSURANCE. "
                        "UPPER_SNAKE_CASE canonical payer name that "
                        "collapses spelling variants. Examples: "
                        "BLUE_CROSS_BLUE_SHIELD (BCBS, BC/BS, BLUE "
                        "CROSS, BLUE SHIELD, BLUECROSSBLUESHIELD, "
                        "BCBSVT, BCBS OF DE, VT BCBS, PRIVATE INSURANCE "
                        "(BCBS)); MEDICARE; RAILROAD_MEDICARE; MEDICAID; "
                        "AETNA; CIGNA; COVENTRY; MVP_HEALTH_CARE; "
                        "UNITED_HEALTHCARE; AMERIHEALTH; ANTHEM; TRICARE; "
                        "HARVARD_PILGRIM; HIGHMARK; CDPHP; TUFTS; "
                        "OXFORD_HEALTH; MAGELLAN; VALUE_OPTIONS; FIDELIS; "
                        "GEISINGER.")
        license_type: Optional[Literal[
            "MEDICAL", "NURSING", "PHARMACY", "SOCIAL_WORK",
            "PHYSICAL_THERAPY", "BUSINESS", "OTHER"
        ]] = Field(
            default=None,
            description="Set only when classification=LICENSE. Broad "
                        "category of professional or business license.")
        reasoning: str = Field(
            description="One short sentence explaining the classification.")

    class BatchClassificationOutput(BaseModel):
        state: str
        classifications: List[PhraseOut] = Field(
            description="One classification per input phrase, in input order.")

    return PhraseIn, BatchClassificationInput, BatchClassificationOutput


_SYSTEM_PROMPT = (
    "You classify NPPES Other Provider Identifier phrases scoped to a "
    "single US state. For each input phrase return exactly one "
    "classification in the same order.\n\n"
    "NPPES Other Provider Identifier slots historically carried payer "
    "enrollment IDs, but providers also used them for state licenses, "
    "NPI, TAX ID, CLIA, CAQH, and other credentials. type_code=\"05\" "
    "always means Medicaid; type_code=\"01\" (labelled OTHER) is a bag "
    "of everything else. The issuer text is the primary signal.\n\n"
    "Categories:\n"
    "  - INSURANCE: payer enrollment IDs. Medicare (original, Railroad "
    "    Medicare, Palmetto GBA, PTAN). Medicaid (any state). "
    "    Commercial (BCBS, Aetna, Cigna, Coventry, MVP, United "
    "    Healthcare, Amerihealth, Anthem, TRICARE/CHAMPUS, Harvard "
    "    Pilgrim, Highmark, CDPHP, Tufts, Oxford, Magellan, Value "
    "    Options, Fidelis, Geisinger, ConnectiCare, Unison, Great "
    "    West, HealthNet, Johns Hopkins, Gateway, Personal Choice, "
    "    CompSych, Fletcher Allen Preferred, Davis Vision, Tufts "
    "    Health Plan, TVHP, AmeriChoice, Diamond State Partners, "
    "    Delaware Physicians Care, Vermont Managed Care, Ladies "
    "    First).\n"
    "  - LICENSE: state medical/nursing/pharmacy/social-work/PT/business "
    "    licenses. \"STATE LICENSE\", \"MEDICAL LICENSE\", \"VT "
    "    LICENSE\", \"BOARD OF PHARMACY\", \"RN LICENSE\", "
    "    \"PHARMACIST LICENSE\", \"STATE OF VERMONT\", \"PROFESSIONAL "
    "    LICENSE\", etc.\n"
    "  - PROVIDER_ID: NPI, INDIVIDUAL NPI, GROUP NPI, TAX ID, CAQH, "
    "    NCPDP, NABP, UPIN.\n"
    "  - FACILITY_ID: CLIA, DME.\n"
    "  - UNKNOWN: only when the phrase is genuinely uninterpretable "
    "    (cryptic 2-3 letter abbreviations you cannot resolve; \"N/A\", "
    "    \"NONE\", \"PROVIDER NUMBER\", \"OTHER ID NUMBER\"). Avoid "
    "    UNKNOWN when you can reasonably infer a category from context.\n\n"
    "When classification=INSURANCE, ALWAYS set payer_type and "
    "canonical_payer_name. Collapse variants aggressively: BC/BS, BCBS, "
    "BLUE CROSS, BLUE SHIELD, BLUE CROSS BLUE SHIELD, BCBSVT, VT BCBS, "
    "BLUECROSSBLUESHIELD, PRIVATE INSURANCE (BCBS), CAREFIRST BCBS all "
    "→ BLUE_CROSS_BLUE_SHIELD (payer_type=COMMERCIAL). RAIL ROAD "
    "MEDICARE, RR MEDICARE, MEDICARE RAILROAD → RAILROAD_MEDICARE "
    "(payer_type=MEDICARE). MHN, HEALTHNET, MANAGED HEALTH NET → "
    "MANAGED_HEALTH_NET.\n\n"
    "When classification=LICENSE, ALWAYS set license_type."
)


def _run_llm_batch(state: str, phrases: list[dict], phrase_cls, input_cls, output_cls) -> list[dict]:
    from pydantic_ai import Agent

    api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ChatHealthyException(
            mode="runtime_error",
            message="classify_other_identifier_phrases: GEMINI_API_KEY / "
                    "GOOGLE_API_KEY not set",
            component="other_identifier_phrases_engine",
            state=state,
        )
    os.environ.setdefault("GOOGLE_API_KEY", api_key)

    agent = Agent(
        _LLM_MODEL_ID,
        result_type=output_cls,
        system_prompt=_SYSTEM_PROMPT,
    )
    batch_in = input_cls(
        state=state,
        phrases=[
            phrase_cls(
                type_code=p["type_code"],
                issuer_text=p["issuer_text"],
                sample_identifiers=p.get("sample_identifiers", []),
            )
            for p in phrases
        ],
    )

    result = agent.run_sync(batch_in.model_dump_json())

    got = result.output if hasattr(result, "output") else result.data
    by_key: dict[tuple[str, str], Any] = {}
    for c in got.classifications:
        by_key[(c.type_code, c.issuer_text.upper())] = c

    out: list[dict] = []
    for p in phrases:
        c = by_key.get((p["type_code"], p["issuer_text"]))
        if c is None:
            out.append({
                "classification": "UNKNOWN",
                "payer_type": None,
                "canonical_payer_name": None,
                "license_type": None,
                "reasoning": "LLM did not return a classification for this phrase.",
            })
            continue
        out.append({
            "classification": c.classification,
            "payer_type": c.payer_type,
            "canonical_payer_name": c.canonical_payer_name,
            "license_type": c.license_type,
            "reasoning": c.reasoning,
        })
    return out


def classify_other_identifier_phrases(config: dict, *, mongo, blob=None) -> dict:
    """Classify all unclassified staging rows for the partition's state.

    Config keys used:
      - run_id
      - partition.business_address_state
    """
    run_id = config["run_id"]
    partition = config.get("partition") or {}
    if not partition:
        raise ChatHealthyException(
            mode="runtime_error",
            message="per-state fanout required but config['partition'] is empty",
            component="other_identifier_phrases_engine",
        )
    state = (partition.get("business_address_state") or "").upper().strip()
    if not state:
        raise ChatHealthyException(
            mode="runtime_error",
            message="classify_other_identifier_phrases: partition lacks "
                    "business_address_state; per-state fanout is required.",
            component="other_identifier_phrases_engine",
        )
    staging_coll = _resolve_staging_collection(mongo)
    phrase_cls, input_cls, output_cls = _build_pydantic_ai_schemas()

    pending = list(staging_coll.find({
        "state": state,
        "run_id": run_id,
        "classification": None,
    }))
    if not pending:
        return {"state": state, "classified": 0, "pending": 0}

    payload = [
        {
            "type_code": d["type_code"],
            "issuer_text": d["issuer_text"],
            "sample_identifiers": d.get("sample_identifiers", []),
        }
        for d in pending
    ]
    results = _run_llm_batch(state, payload, phrase_cls, input_cls, output_cls)

    classified = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    for doc, cls in zip(pending, results):
        staging_coll.update_one(
            {"_id": doc["_id"], "classification": None},
            {
                "$set": {
                    "classification": cls["classification"],
                    "payer_type": cls["payer_type"],
                    "canonical_payer_name": cls["canonical_payer_name"],
                    "license_type": cls["license_type"],
                    "reasoning": cls["reasoning"],
                    "classified_at": now_iso,
                    "classified_by": _LLM_MODEL_NAME_FOR_AUDIT,
                }
            },
        )
        classified += 1

    return {"state": state, "classified": classified, "pending": len(pending)}


# ── Stage 3: apply ─────────────────────────────────────────────────────────

def _state_classification_lookup(staging_coll, state: str, run_id: str) -> dict[str, dict]:
    """Return {phrase_state_id: staging_doc} for classified rows in state."""
    lookup: dict[str, dict] = {}
    query = {"state": state, "run_id": run_id, "classification": {"$ne": None}}
    for doc in staging_coll.find(query):
        lookup[doc["_id"]] = doc
    return lookup


def _route_entry(entry: dict, cls: dict | None, state: str) -> tuple[str, dict]:
    """Decide the destination of one other_identifier entry."""
    identifier = (entry.get("identifier") or "").strip()
    entry_state = (entry.get("state") or "").strip().upper() or state
    issuer_raw = (entry.get("issuer") or "").strip()

    if cls is None or cls.get("classification") in (None, "UNKNOWN"):
        retained = dict(entry)
        retained["classification_pending"] = True
        return ("other", retained)

    kind = cls["classification"]

    if kind == "INSURANCE":
        promoted = {
            "insurance_type": cls.get("payer_type") or "COMMERCIAL",
            "payer_name": cls.get("canonical_payer_name") or "COMMERCIAL_UNSPECIFIED",
            "payer_id": identifier,
            "source": "nppes_other_identifiers",
        }
        if entry_state:
            promoted["state"] = entry_state
        if issuer_raw:
            promoted["issuer_raw"] = issuer_raw
        return ("insurance", promoted)

    if kind == "LICENSE":
        promoted = {
            "number": identifier,
            "source": "nppes_other_identifiers",
        }
        if cls.get("license_type"):
            promoted["license_type"] = cls["license_type"]
        if entry_state:
            promoted["state"] = entry_state
        if issuer_raw:
            promoted["issuer_raw"] = issuer_raw
        return ("license", promoted)

    retained = dict(entry)
    retained["classification"] = kind
    return ("other", retained)


def apply_other_identifier_classifications(config: dict, *, mongo, blob=None) -> dict:
    """Rewrite provider records in the partition's state per the
    staging table's classifications.

    Config keys used:
      - run_id
      - provider_collection: "<db>.<coll>" on pipeline cluster
      - partition.business_address_state
    """
    from record_subdoc_dedup import merge_insurance, merge_licenses

    run_id = config["run_id"]
    partition = config.get("partition") or {}
    if not partition:
        raise ChatHealthyException(
            mode="runtime_error",
            message="per-state fanout required but config['partition'] is empty",
            component="other_identifier_phrases_engine",
        )
    state = (partition.get("business_address_state") or "").upper().strip()
    if not state:
        raise ChatHealthyException(
            mode="runtime_error",
            message="apply_other_identifier_classifications: partition lacks "
                    "business_address_state; per-state fanout is required.",
            component="other_identifier_phrases_engine",
        )
    provider_coll = _resolve_provider_collection(config, mongo)
    staging_coll = _resolve_staging_collection(mongo)
    lookup = _state_classification_lookup(staging_coll, state, run_id)

    query = {
        "addresses": {"$elemMatch": {"address_type": "business", "state": state}},
        "other_identifiers": {"$exists": True, "$ne": []},
    }

    scanned = 0
    updated = 0
    promoted_insurance = 0
    promoted_license = 0
    retained_other = 0
    pending_unknown = 0

    for doc in provider_coll.find(query, no_cursor_timeout=True):
        scanned += 1
        new_insurance: list[dict] = []
        new_licenses: list[dict] = []
        new_others: list[dict] = []
        for entry in doc.get("other_identifiers") or []:
            if not isinstance(entry, dict):
                continue
            tc = (entry.get("type_code") or "").strip()
            it = (entry.get("issuer") or "").strip().upper()
            pid = _phrase_state_id(tc, it, state)
            cls = lookup.get(pid)
            kind, out = _route_entry(entry, cls, state)
            if kind == "insurance":
                new_insurance.append(out)
                promoted_insurance += 1
            elif kind == "license":
                new_licenses.append(out)
                promoted_license += 1
            else:
                new_others.append(out)
                if out.get("classification_pending"):
                    pending_unknown += 1
                else:
                    retained_other += 1

        merged_insurance = merge_insurance(doc.get("insurance"), new_insurance)
        merged_licenses = merge_licenses(doc.get("licenses"), new_licenses)

        set_op: dict[str, Any] = {}
        unset_op: dict[str, str] = {}
        if new_others:
            set_op["other_identifiers"] = new_others
        elif "other_identifiers" in doc:
            unset_op["other_identifiers"] = ""
        if merged_insurance:
            set_op["insurance"] = merged_insurance
        elif "insurance" in doc:
            unset_op["insurance"] = ""
        if merged_licenses:
            set_op["licenses"] = merged_licenses
        elif "licenses" in doc:
            unset_op["licenses"] = ""

        op: dict[str, Any] = {}
        if set_op:
            op["$set"] = set_op
        if unset_op:
            op["$unset"] = unset_op
        if op:
            provider_coll.update_one({"_id": doc["_id"]}, op)
            updated += 1

    return {
        "state": state,
        "providers_scanned": scanned,
        "providers_updated": updated,
        "promoted_insurance": promoted_insurance,
        "promoted_license": promoted_license,
        "retained_other": retained_other,
        "pending_unknown": pending_unknown,
    }
