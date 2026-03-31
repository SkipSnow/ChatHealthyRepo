# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# main.py — ChatHealthy.ai FindCare backend entry point.
#
# ARCH-001: Modular monolith. All business logic lives in domain/ services.
# This file is the host adapter: FastAPI setup, service wiring, chat loop.
# HuggingFace surface: thinnest possible. No business logic here.
#
# Authored by Claude Code (Claude Opus 4.6)
# Architecture by GPT-5.3 (Enterprise Architect)
# Supervised by Skip Snow, Founder & CEO

import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
from pypdf import PdfReader
from url_guardian import URLGuardian

# ARCH-001 imports — domain services and facades
from application.tool_router import ToolRouter
from application.facades.find_care_facade import FindCareFacade
from application.facades.evaluate_care_facade import EvaluateCareFacade
from domain.find_care.provider_search_service import ProviderSearchService
from domain.find_care.specialty_service import SpecialtyService
from domain.evaluate_care_quality.clinical_trials_service import ClinicalTrialsService
from domain.evaluate_care_quality.provider_detail_service import ProviderDetailService
from domain.shared.safety.safety_service import SafetyService
from domain.shared.consent.consent_service import ConsentService
from domain.shared.lead_capture.lead_service import LeadService
from domain.shared.unknowns.unknown_question_service import UnknownQuestionService
from domain.shared.content.about_service import AboutService
from application.tool_models.provider_search_models import ProviderSearchInput, SpecialtyInput
from application.tool_models.clinical_trials_models import ClinicalTrialsInput, ProviderDetailInput
from application.tool_models.consent_models import LeadInput, UnknownInput

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Shared utilities — MongoDB connection manager
# Source: Code/Shared/ChatHealthyMongoUtilities.py
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "Shared"))
from ChatHealthyMongoUtilities import ChatHealthyMongoUtilities

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
_log = logging.getLogger("findcare")

# ---------------------------------------------------------------------------
# Configuration — all from environment variables
# ---------------------------------------------------------------------------
_ENV_PREFIX    = os.getenv("ENV_PREFIX", "dev")
_DEBUG         = os.getenv("DEBUG", "false").lower() == "true"
_HUMAN_TESTING = os.getenv("HUMAN_TESTING", "false").lower() == "true"
_APP_VERSION   = os.getenv("APP_VERSION", "unknown")

EMERGENCY_RESPONSE = (
    "<b>Call 911 or go to the nearest emergency room immediately. Do not wait.</b>\n\n"
    "<b>This chat has been suspended.</b>"
)

EMERGENCY_KEYWORDS = [
    "chest pain", "chest tightness", "heart attack",
    "can't breathe", "cannot breathe", "difficulty breathing", "trouble breathing",
    "not breathing", "stopped breathing",
    "stroke", "face drooping", "arm weakness", "sudden numbness",
    "severe bleeding", "bleeding out", "won't stop bleeding",
    "unconscious", "passed out", "unresponsive",
    "overdose", "took too many", "took too much",
    "suicide", "suicidal", "kill myself", "end my life",
    "seizure", "convulsing",
    "severe allergic reaction", "anaphylaxis", "throat closing",
    "choking",
]

# ---------------------------------------------------------------------------
# MongoDB — lazy connection via ChatHealthyMongoUtilities
# Source: MONGO_FRONTEND_connectionString in .env
# Reads only — Chat app never writes to PublicHealthData (GOV-005)
# ---------------------------------------------------------------------------
_mongo_frontend_str = os.getenv("MONGO_FRONTEND_connectionString") or ""
_db_manager = None
_db_unavailable = False


def _get_db():
    """Lazy MongoDB connection. Returns MongoClient or None."""
    global _db_manager, _db_unavailable
    if not _mongo_frontend_str or _db_unavailable:
        return None
    try:
        if _db_manager is None:
            _db_manager = ChatHealthyMongoUtilities(_mongo_frontend_str)
        return _db_manager.getConnection()
    except Exception as e:
        _log.warning("MongoDB unavailable: %s", e)
        _db_manager = None
        _db_unavailable = True
        return None


# ---------------------------------------------------------------------------
# Notification utility — sends email via SparkPost
# Used by: LeadService, UnknownQuestionService
# ---------------------------------------------------------------------------
_SPARKMAIL_API_KEY = os.getenv("SPARKMAIL_API_KEY", "")
_SPARKMAIL_FROM    = os.getenv("NOTIFICATION_FROM_EMAIL", "")
_SPARKMAIL_TO      = os.getenv("NOTIFICATION_TO_EMAIL", "")


def push(message):
    """Send a notification email. Fire-and-forget."""
    if not _SPARKMAIL_API_KEY:
        return
    try:
        from sparkpost import SparkPost
        SparkPost(_SPARKMAIL_API_KEY).transmissions.send(
            recipients=[_SPARKMAIL_TO],
            from_email=_SPARKMAIL_FROM,
            subject="ChatHealthy — Activity",
            text=message,
        )
    except Exception as exc:
        _log.warning("SparkPost send failed: %s", exc)


# ---------------------------------------------------------------------------
# Database write utility — thin wrapper over ChatHealthyMongoUtilities.commit()
# Used by: LeadService, UnknownQuestionService, ToolRouter
# ---------------------------------------------------------------------------
def commitSignificantActivity(payload=None, **kwargs):
    """Write a record to MongoDB via ChatHealthyMongoUtilities.commit()."""
    if _get_db() is None:
        return {"recorded": "ok", "note": "MongoDB unavailable"}
    try:
        payload = payload or kwargs
        if isinstance(payload, str):
            payload = json.loads(payload)
        return _db_manager.commit(_ENV_PREFIX, payload["database"], payload["collection"], payload["record"])
    except Exception as exc:
        _log.error("commitSignificantActivity failed: %s", exc)
        return {"recorded": "error", "note": str(exc)}


# ---------------------------------------------------------------------------
# Chat history formatting — used by ToolRouter for consent tools
# ---------------------------------------------------------------------------
def _format_chat_history(messages, truncate: bool = True):
    """Format chat history. truncate=True for safety/debug (500 chars), False for verbatim consent."""
    max_len = 500 if truncate else None
    formatted = []
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            content = " ".join(parts)
        content = str(content)
        if max_len and len(content) > max_len:
            content = content[:max_len] + "..."
        formatted.append({"role": m.get("role", ""), "content": content})
    return formatted


# ---------------------------------------------------------------------------
# AI helpers — used during service initialization
# Source: OpenAI API for embeddings, Anthropic API for query expansion
# ---------------------------------------------------------------------------
_oai_client: Optional[OpenAI] = None


def _get_oai_client() -> OpenAI:
    global _oai_client
    if _oai_client is None:
        _oai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _oai_client


def _get_query_embedding(text: str) -> Optional[list]:
    """Embed text via OpenAI text-embedding-3-large. Used by ProviderSearchService."""
    try:
        resp = _get_oai_client().embeddings.create(model="text-embedding-3-large", input=text)
        return resp.data[0].embedding
    except Exception as e:
        _log.warning("Embedding query failed: %s", e)
        return None


def _get_specialty_vector(query: str) -> Optional[list]:
    """Embed text via OpenAI text-embedding-3-small. Used by SpecialtyService."""
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        return client.embeddings.create(model="text-embedding-3-small", input=query).data[0].embedding
    except Exception as e:
        _log.warning("Specialty embedding failed: %s", e)
        return None


def _expand_query_terms(query: str) -> list[str]:
    """AI-powered query expansion for specialty search. Uses Claude Haiku."""
    try:
        client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": (
                "You are helping search a medical provider taxonomy database. "
                "Given the specialty query, return a JSON array of 2-5 lowercase keyword stems "
                "to search for in Specialization and Display Name fields. "
                "Return ONLY the JSON array, no other text.\n\n"
                "Examples:\n"
                "Query: pediatrician -> [\"pediatric\", \"child\"]\n"
                "Query: cardiologist -> [\"cardio\", \"cardiac\", \"cardiovascular\"]\n"
                "Query: OB-GYN -> [\"obstetric\", \"gynecolog\", \"maternal\", \"fetal\"]\n\n"
                f"Query: {query}"
            )}],
        )
        raw = response.content[0].text.strip() if response.content else ""
        return json.loads(raw) if raw else []
    except Exception as exc:
        _log.warning("_expand_query_terms failed for %r: %s", query, exc)
        return []


def _trim(text: str, max_chars: int) -> str:
    """Trim text to max_chars with truncation note."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... truncated at {max_chars} chars ...]"


# ---------------------------------------------------------------------------
# ME context — loads founder/company content at startup
# Source: PDF + text files in me/ directory
# Changed: loaded once, injected into AboutService
# ---------------------------------------------------------------------------
_ME_DIR = os.getenv("ME_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "me"
)
if not os.path.isdir(_ME_DIR):
    _ME_DIR = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", "ChatHealthyWhoAmIChat", "me"
    )


def _load_me_context():
    ctx = {}
    try:
        reader = PdfReader(os.path.join(_ME_DIR, "SkipSnowLinkedInProfile.pdf"))
        ctx["linkedin"] = "".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        ctx["linkedin"] = ""
        _log.warning("ME_DIR LinkedIn load failed: %s", e)
    try:
        with open(os.path.join(_ME_DIR, "anthropic_principles.txt"), "r", encoding="utf-8") as f:
            ctx["anthropic_principles"] = f.read()
    except Exception as e:
        ctx["anthropic_principles"] = ""
    try:
        with open(os.path.join(_ME_DIR, "summary.txt"), "r", encoding="utf-8") as f:
            ctx["summary"] = f.read()
    except Exception as e:
        ctx["summary"] = ""
    try:
        reader = PdfReader(os.path.join(_ME_DIR, "chatHealthy_ai_business_plan.pdf"))
        ctx["business_plan"] = "".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        ctx["business_plan"] = ""
    return ctx


_ME = _load_me_context()
_url_guardian = URLGuardian(cache_ttl=3600, request_timeout=5)

# ---------------------------------------------------------------------------
# Build number — read from MongoDB once at startup
# Source: {ENV_PREFIX}_System.build_counter
# Changed: incremented by ops/bump_build.py on deploy only
# ---------------------------------------------------------------------------
def _get_build_number() -> str:
    try:
        db = _get_db()
        if db is None:
            return "?"
        record = db[f"{_ENV_PREFIX}_System"]["build_counter"].find_one({"_id": "build"})
        return str(record["number"]) if record else "0"
    except Exception as exc:
        _log.warning("Build counter read failed: %s", exc)
        return "?"


_BUILD = _get_build_number()

# ---------------------------------------------------------------------------
# Welcome message — production vs UAT
# Source: UAT report from Code/Shared/ops/uat_report.py (reads from MongoDB)
# ---------------------------------------------------------------------------
WELCOME_MESSAGE = (
    "**Welcome to ChatHealthy FindCare**\n\n"
    "Here's what I can help you with:\n\n"
    "- **Find a doctor** — search for providers by specialty or condition\n"
    "  - Delaware, Mississippi, Virginia\n"
    "- **Get provider details** — credentials, license, NPI data, and research links\n"
    "- **Identify the right specialty** — describe your situation\n"
    "- **Clinical trials** — find recruiting research studies for any condition\n"
    "  - Find distance and travel time from any location to trial sites\n"
    "- **About ChatHealthy** — our mission, team, and platform\n"
    "- **Contact us** — request a follow-up from the ChatHealthy team\n\n"
    "**What can I help you with today?**"
)

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "Shared", "ops"))
from uat_report import build_uat_welcome


def _build_test_welcome():
    env_label = _ENV_PREFIX if os.getenv("SPACE_ID") else "local"
    return build_uat_welcome(build=_BUILD, version=_APP_VERSION, env=env_label, db=_get_db(), env_prefix=_ENV_PREFIX)


# ---------------------------------------------------------------------------
# System prompt — rules for Claude Sonnet during conversation
# Source: built dynamically, references tool names (not class names)
# Changed: ToolRouter is source of truth for available tools
# ---------------------------------------------------------------------------
def _system_prompt(follow_up_check: bool = False) -> str:
    name    = "Skip Snow"
    website = "ChatHealthy.AI"
    base = (
        f"You are acting as {name}. You are answering questions on {website}'s website, "
        f"particularly questions related to {name}'s career, background, skills and experience and plans for the future of this web site. "
        f"Your responsibility is to represent {name} and {website} for interactions on the website as faithfully as possible. "
        f"Be professional and engaging, as if talking to a potential client or future employer who came across the website. "
        f"\n\n## STRICT ANSWER RULES — NO EXCEPTIONS\n"
        f"RULE 0 — EMERGENCY: If the user describes ANY symptom or situation that could be a medical emergency, "
        f"STOP immediately. Do not use any tool. Respond ONLY with the exact text: '{EMERGENCY_RESPONSE}'\n"
        f"RULE 1 — CONTEXT TOOLS (only when user asks for information): "
        f"If the user explicitly asks about {name}'s background, career, or qualifications — call get_skip_snow_context first. "
        f"If the user explicitly asks about {website}'s mission, business, or platform — call get_chathealthy_context first. "
        f"Do NOT call these tools for greetings, casual conversation, or messages that do not ask for information. "
        f"Answer ONLY from what those tools return. Never use general training knowledge to answer. "
        f"CRITICAL: When the tool result contains a 'connect' field, output it EXACTLY as-is — it contains pre-formatted markdown links. "
        f"Do NOT rewrite links as plain text. The connect field is: [Skip Snow on LinkedIn](https://linkedin.com/in/skipsnow) — output this EXACT markdown.\n"
        f"RULE 2 — UNANSWERABLE QUESTIONS: "
        f"You know ONLY what is provided to you in this session: "
        f"{name}'s career and background (from get_skip_snow_context), "
        f"{website}'s mission and platform (from get_chathealthy_context), "
        f"healthcare providers in DE, MS, and VA (from find_providers), "
        f"medical specialties (from find_specialty_codes), "
        f"recruiting clinical trials (from search_clinical_trials). "
        f"You know NOTHING else. This rule applies to EVERY user message regardless of conversation history or context. "
        f"Even if previous messages were about healthcare, each new message must be evaluated independently. "
        f"For ANY question not answerable from the above sources, call record_unknown_question with the question and classification. "
        f"Do not attempt to answer from general knowledge. Do not use training data. "
        f"DO NOT answer the question. Call record_unknown_question and present the template VERBATIM.\n"
        f"RULE 3 — MEDICAL ADVICE: You MUST decline all personal medical advice requests. "
        f"You CANNOT prescribe, diagnose, or recommend treatment. You CAN help users navigate: find providers, find specialists, search clinical trials. "
        f"If a user asks for medical advice, call record_unknown_question with question_class 'medical_advice'.\n"
        f"RULE 4 — PROVIDER RESULTS: When presenting provider results from find_providers, ALWAYS format as a bullet list. "
        f"NEVER use a markdown table for provider results. Use this exact format for each provider:\n"
        f"- **Provider Name**\n  - Address\n  - County\n  - Phone\n  - NPI: number\n"
        f"RULE 5 — PROVIDER DETAIL: When a user asks about a specific provider (ratings, background, credentials), "
        f"call lookup_provider_external with the provider's name and NPI.\n"
        f"RULE 6 — CLINICAL TRIAL TRAVEL: When showing clinical trial results, after the first pass "
        f"ask the user: 'Would you like to know the travel distance and estimated drive time to these trial sites?' "
        f"If yes, call search_clinical_trials again with the same condition and the user's location in user_location. "
        f"The user's location can be ANYWHERE in the world — US or international. Do NOT assume US-only. "
        f"Pass whatever location the user provides (city, state, country). Google Routes handles international addresses. "
        f"Present the travel_info (distance and drive time) for each trial site. "
        f"Do NOT include travel info on the first call — only when the user requests it.\n\n"
    )
    if follow_up_check:
        base += (
            f"RULE 7 — FOLLOW-UP OFFER: It has been a while since you asked. "
            f"After answering this question, ask the user: "
            f"'Would you like someone from {website} to follow up with you personally?' "
            f"If yes: collect their name and email, then complete the two-tier consent flow before calling record_user_details.\n"
        )
    return base


# ---------------------------------------------------------------------------
# Tool definitions — Anthropic format (schema only, dispatch via ToolRouter)
# Source: defined here, dispatched by ToolRouter to domain services
# Changed: globals().get() eliminated (F-05). ToolRouter is the authority.
# ---------------------------------------------------------------------------
anthropic_tools = [
    {
        "name": "find_providers",
        "description": (
            "Search for healthcare providers (doctors, specialists) in a specific US state. "
            "FindCare currently covers Delaware (DE), Mississippi (MS), and Virginia (VA). "
            "Call this when the user asks to find a doctor, specialist, or provider in a location."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "specialty_query": {"type": "string"},
                "state": {"type": "string"},
                "city": {"type": "string"},
                "county": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["specialty_query", "state"],
        },
    },
    {
        "name": "record_user_details",
        "description": (
            "Record a user's contact details after obtaining email. "
            "Before calling this tool you MUST complete the two-tier consent flow."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "email": {"type": "string"},
                "name": {"type": "string"},
                "notes": {"type": "string"},
                "message": {"type": "string"},
                "consent_verbatim": {"type": "boolean"},
                "consent_summary": {"type": "boolean"},
            },
            "required": ["email", "notes", "consent_verbatim"],
        },
    },
    {
        "name": "record_unknown_question",
        "description": (
            "Call this tool when the question is not answerable from your sources. "
            "Classify the question and present the response_template VERBATIM."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "question_class": {
                    "type": "string",
                    "enum": ["healthcare_capability", "medical_advice", "irrelevant"],
                },
                "consent": {"type": "boolean"},
            },
            "required": ["question", "question_class"],
        },
    },
    {
        "name": "find_specialty_codes",
        "description": "Look up medical specialty taxonomy codes (NUCC).",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_clinical_trials",
        "description": "Search for actively recruiting clinical trials on ClinicalTrials.gov.",
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "location": {"type": "string"},
                "user_location": {"type": "string", "description": "User's location for travel time — any location worldwide."},
                "max_results": {"type": "integer"},
            },
            "required": ["condition"],
        },
    },
    {
        "name": "get_skip_snow_context",
        "description": "Return Skip Snow's professional background, career summary, and LinkedIn profile.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_chathealthy_context",
        "description": "Return ChatHealthy.AI company context: business plan and operating principles.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "lookup_provider_external",
        "description": "Look up provider details from NPI Registry and research links.",
        "input_schema": {
            "type": "object",
            "properties": {
                "provider_name": {"type": "string"},
                "npi": {"type": "string"},
                "state": {"type": "string"},
            },
            "required": ["provider_name"],
        },
    },
]

# ---------------------------------------------------------------------------
# ARCH-001: Domain service initialization
# Source: domain/ classes, injected with dependencies from this file
# Changed: all business logic now lives in service classes, not here
# ---------------------------------------------------------------------------

# FindCare business component
_provider_search_service = ProviderSearchService(
    get_db_fn=_get_db, env_prefix=_ENV_PREFIX, get_embedding_fn=_get_query_embedding,
)
_specialty_service = SpecialtyService(
    get_db_fn=_get_db, env_prefix=_ENV_PREFIX, expand_query_fn=_expand_query_terms, get_vector_fn=_get_specialty_vector,
)
_find_care_facade = FindCareFacade(provider_search=_provider_search_service, specialty=_specialty_service)

# EvaluateCareQuality business component
_clinical_trials_service = ClinicalTrialsService()
_provider_detail_service = ProviderDetailService()
_evaluate_care_facade = EvaluateCareFacade(
    clinical_trials=_clinical_trials_service, provider_detail=_provider_detail_service, find_care_facade=_find_care_facade,
)

# Shared services
_safety_service = SafetyService(get_db_fn=_get_db, env_prefix=_ENV_PREFIX, emergency_keywords=EMERGENCY_KEYWORDS)
_consent_service = ConsentService()
_lead_service = LeadService(
    get_db_fn=_get_db, env_prefix=_ENV_PREFIX, consent=_consent_service, push_fn=push, commit_fn=commitSignificantActivity,
)
_unknown_question_service = UnknownQuestionService(consent=_consent_service, push_fn=push, commit_fn=commitSignificantActivity)
_about_service = AboutService(me_context=_ME, trim_fn=_trim)

# ---------------------------------------------------------------------------
# ToolRouter — F-05 fix: explicit allowlist replaces globals().get()
# Source: application/tool_router.py
# Changed: Pydantic validation on all tool inputs (Phase 6)
# ---------------------------------------------------------------------------
_tool_router = ToolRouter()
_tool_router.register_with_models([
    ("find_providers",          _find_care_facade.search_providers,            ProviderSearchInput),
    ("find_specialty_codes",    _find_care_facade.identify_specialty,          SpecialtyInput),
    ("search_clinical_trials",  _evaluate_care_facade.search_clinical_trials,  ClinicalTrialsInput),
    ("lookup_provider_external", _evaluate_care_facade.get_provider_details,   ProviderDetailInput),
    ("record_user_details",     _lead_service.record_user_details,             LeadInput),
    ("record_unknown_question", _unknown_question_service.record,              UnknownInput),
    ("get_skip_snow_context",   _about_service.get_skip_snow_context),
    ("get_chathealthy_context", _about_service.get_chathealthy_context),
    ("commitSignificantActivity", commitSignificantActivity),
])
_log.info("ToolRouter initialized: %s", _tool_router.registered_tools)


def _handle_tool_calls(tool_use_blocks, messages):
    """Dispatch tool calls via ToolRouter (F-05: no more globals().get)."""
    return _tool_router.handle_tool_calls(tool_use_blocks, messages, _format_chat_history)


# ---------------------------------------------------------------------------
# Debug logging — dev environment only
# Source: writes to {ENV_PREFIX}_Debug.chat_calls in MongoDB
# Changed: uses ConsentService.de_identify instead of legacy deIdentify()
# ---------------------------------------------------------------------------
def _debug_log_chat(
    ip: str, message: str, history_len: int, tool_loop_iters: int,
    tokens_in: Optional[int], tokens_out: Optional[int],
    response_text: Optional[str], error: Optional[str], history: Optional[list] = None,
) -> None:
    db = _get_db()
    if db is None:
        return
    try:
        record = {
            "datetime": datetime.now(timezone.utc).isoformat(),
            "ip": ip, "message_preview": message[:200],
            "history_len": history_len, "tool_loop_iters": tool_loop_iters,
            "tokens_in": tokens_in, "tokens_out": tokens_out,
            "response_preview": response_text[:200] if response_text else None,
            "error": error,
        }
        if error and history:
            safe_history = [
                {"role": m.get("role", ""), "content": str(m.get("content", ""))[:500]}
                for m in history if m.get("role") in ("user", "assistant")
            ]
            _consent_service.de_identify(safe_history)
            record["chat_history_deidentified"] = safe_history
        db[f"{_ENV_PREFIX}_Debug"]["chat_calls"].insert_one(record)
    except Exception as exc:
        _log.warning("debug_log_chat failed: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------
app = FastAPI(title="ChatHealthy FindCare API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chathealthy.ai", "https://www.chathealthy.ai", "https://dev.chathealthy.ai",
    ],
    allow_origin_regex=r"http://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    response: Optional[str] = None
    emergency: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


@app.get("/welcome")
def welcome():
    if _HUMAN_TESTING:
        return {"message": _build_test_welcome()}
    return {"message": WELCOME_MESSAGE}


@app.get("/health")
def health():
    db_ok = _get_db() is not None
    env_label = _ENV_PREFIX if os.getenv("SPACE_ID") else "local"
    return {"status": "ok", "db": "connected" if db_ok else "unavailable",
            "env": env_label, "build": _BUILD, "version": _APP_VERSION}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")
    try:
        return await _chat_inner(body, request)
    except Exception as e:
        tb = traceback.format_exc()
        _log.error("CHAT ERROR: %s\n%s", e, tb)
        err_str = str(e)
        if "429" in err_str or "rate_limit" in err_str.lower():
            err_type, err_msg = "rate_limit", f"Rate limit hit — {err_str}"
        elif "unavailable" in err_str.lower() or "connection" in err_str.lower():
            err_type, err_msg = "db_unavailable", err_str
        else:
            err_type, err_msg = "internal", tb if _DEBUG else err_str
        _debug_log_chat(ip, body.message, len(body.history), 0, None, None, None, err_msg, body.history)
        return ChatResponse(error=err_msg, error_type=err_type)


# ---------------------------------------------------------------------------
# Chat loop — the core conversation handler
# Source: Anthropic Claude Sonnet 4.6 with tool-use
# Changed: safety via SafetyService, tools via ToolRouter, de-identify via ConsentService
# ---------------------------------------------------------------------------
async def _chat_inner(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")

    # Safety: admin unlock, IP lock check, emergency detection (via SafetyService)
    if _safety_service.try_admin_unlock(body.message, ip):
        return ChatResponse(response="Session unlocked.")

    history = body.history

    if _safety_service.is_ip_locked(ip):
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    if _safety_service.is_emergency(body.message):
        full_history = list(history) + [{"role": "user", "content": body.message}]
        _safety_service.lock_ip(ip, trigger_message=body.message, history=full_history)
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    user_msg_count = sum(1 for m in history if m.get("role") == "user")
    follow_up      = user_msg_count > 0 and user_msg_count % 5 == 0
    system         = _system_prompt(follow_up_check=follow_up)

    messages = list(history) + [{"role": "user", "content": body.message}]
    anthropic_client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))

    total_tokens_in = total_tokens_out = 0

    _log.info("CHAT call=initial msgs=%d", len(messages))
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4096,
        system=system, messages=messages, tools=anthropic_tools,
    )
    total_tokens_in  += getattr(response.usage, "input_tokens", 0)
    total_tokens_out += getattr(response.usage, "output_tokens", 0)

    # Tool-use loop — Claude selects tools, ToolRouter dispatches to services
    loop_iter = 0
    while response.stop_reason == "tool_use":
        tool_uses    = [b for b in response.content if b.type == "tool_use"]
        tool_names   = [b.name for b in tool_uses]
        tool_results = _handle_tool_calls(tool_uses, messages)

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user",      "content": tool_results})

        loop_iter += 1
        _log.info("CHAT call=tool_loop iter=%d tools=%s msgs=%d", loop_iter, tool_names, len(messages))
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4096,
            system=system, messages=messages, tools=anthropic_tools,
        )
        total_tokens_in  += getattr(response.usage, "input_tokens", 0)
        total_tokens_out += getattr(response.usage, "output_tokens", 0)

    text = next((b.text for b in response.content if b.type == "text"), "")
    text = _url_guardian.guard_text(text)

    # HACK: Force LinkedIn URL — Sonnet strips markdown links (PE bug, 3 prompt attempts failed)
    text = re.sub(r'(?<!\[)Skip Snow on LinkedIn(?!\])',
                  '[Skip Snow on LinkedIn](https://linkedin.com/in/skipsnow)', text)

    _log.info("CHAT complete tokens_in=%d tokens_out=%d", total_tokens_in, total_tokens_out)
    _debug_log_chat(ip, body.message, len(history), loop_iter, total_tokens_in, total_tokens_out, text, None)
    return ChatResponse(response=text, tokens_in=total_tokens_in, tokens_out=total_tokens_out)


# ---------------------------------------------------------------------------
# Serve React frontend — dynamic index.html (no caching), static assets cached
# ---------------------------------------------------------------------------
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    from starlette.staticfiles import StaticFiles
    from starlette.responses import FileResponse

    @app.get("/")
    async def serve_index():
        return FileResponse(
            os.path.join(_static_dir, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"},
        )

    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
