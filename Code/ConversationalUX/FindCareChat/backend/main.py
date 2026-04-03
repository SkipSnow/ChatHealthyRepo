# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# main.py — ChatHealthy.ai FindCare backend. Host adapter only.
#
# ARCH-001: All business logic in domain/ services. All config in PromptSystemMaker.
# This file: FastAPI setup, service wiring, chat loop. Nothing else.

import json
import logging
import os
import re
import sys
import traceback
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from url_guardian import URLGuardian

# ARCH-001 — domain services
from application.tool_router import ToolRouter
from application.facades.evaluate_care_facade import EvaluateCareFacade
from domain.find_care.provider_search_service import FindCareService
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
from infrastructure.embeddings.embedding_client import EmbeddingClient
from infrastructure.debug_logger import DebugLogger

load_dotenv(override=True)

# Shared utilities
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "Shared"))
from ChatHealthyMongoUtilities import ChatHealthyMongoUtilities
from prompt_system_maker import PromptSystemMaker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if os.getenv("DEBUG", "false").lower() == "true" else logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
_log = logging.getLogger("findcare")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_ENV_PREFIX    = os.getenv("ENV_PREFIX", "dev")
_DEBUG         = os.getenv("DEBUG", "false").lower() == "true"
_HUMAN_TESTING_RAW = os.getenv("HUMAN_TESTING", "false")
_HUMAN_TESTING = _HUMAN_TESTING_RAW.lower() not in ("false", "0", "")
_APP_VERSION   = os.getenv("APP_VERSION", "unknown")

EMERGENCY_RESPONSE = (
    "<b>Call 911 or go to the nearest emergency room immediately. Do not wait.</b>\n\n"
    "<b>This chat has been suspended.</b>"
)

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
_mongo_frontend_str = os.getenv("MONGO_FRONTEND_connectionString") or ""
_db_manager = None
_db_unavailable = False

def _get_db():
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
# Utilities — push notification + DB write
# ---------------------------------------------------------------------------
_SPARKMAIL_API_KEY = os.getenv("SPARKMAIL_API_KEY", "")
_SPARKMAIL_FROM    = os.getenv("NOTIFICATION_FROM_EMAIL", "")
_SPARKMAIL_TO      = os.getenv("NOTIFICATION_TO_EMAIL", "")

def push(message):
    if not _SPARKMAIL_API_KEY:
        return
    try:
        from sparkpost import SparkPost
        SparkPost(_SPARKMAIL_API_KEY).transmissions.send(
            recipients=[_SPARKMAIL_TO], from_email=_SPARKMAIL_FROM,
            subject="ChatHealthy — Activity", text=message,
        )
    except Exception as exc:
        _log.warning("SparkPost send failed: %s", exc)

def commitSignificantActivity(payload=None, **kwargs):
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

def _format_chat_history(messages, truncate: bool = True):
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
# PromptSystemMaker — loads all config from brain artifacts
# ---------------------------------------------------------------------------
# Brain dir: try local repo structure first, fall back to HuggingFace flat layout
_brain_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "brain")
if not os.path.isdir(_brain_dir):
    _brain_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain")
_prompt_maker = PromptSystemMaker(brain_dir=_brain_dir, env_prefix=_ENV_PREFIX)
EMERGENCY_KEYWORDS = _prompt_maker.load_emergency_keywords()
anthropic_tools = _prompt_maker.load_tool_definitions()
WELCOME_MESSAGE = PromptSystemMaker.build_welcome_message()
_BUILD = PromptSystemMaker.get_build_number(_get_db, _ENV_PREFIX)

_ME_DIR = os.getenv("ME_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "me")
if not os.path.isdir(_ME_DIR):
    _ME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ChatHealthyWhoAmIChat", "me")
_ME = _prompt_maker.load_me_context(_ME_DIR)

_url_guardian = URLGuardian(cache_ttl=3600, request_timeout=5)

# UAT report
# UAT report: local repo path or HF flat layout
_ops_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "Shared", "ops")
if not os.path.isdir(_ops_dir):
    _ops_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ops")
sys.path.insert(0, _ops_dir)
from uat_report import build_uat_welcome

def _build_test_welcome():
    return build_uat_welcome(get_db_fn=_get_db)

def _system_prompt(follow_up_check: bool = False) -> str:
    return _prompt_maker.build_system_prompt(emergency_response=EMERGENCY_RESPONSE, follow_up_check=follow_up_check)

# ---------------------------------------------------------------------------
# Service initialization — ARCH-001
# ---------------------------------------------------------------------------
_embedding_client = EmbeddingClient()

_specialty_service = SpecialtyService(
    get_db_fn=_get_db, env_prefix=_ENV_PREFIX,
    expand_query_fn=_embedding_client.expand_query_terms, get_vector_fn=_embedding_client.get_specialty_vector)
_find_care = FindCareService(
    get_db_fn=_get_db, env_prefix=_ENV_PREFIX,
    get_embedding_fn=_embedding_client.get_query_embedding, specialty_service=_specialty_service)

_clinical_trials_service = ClinicalTrialsService()
_provider_detail_service = ProviderDetailService()
_evaluate_care_facade = EvaluateCareFacade(
    clinical_trials=_clinical_trials_service, provider_detail=_provider_detail_service, find_care_facade=_find_care)

_safety_service = SafetyService(get_db_fn=_get_db, env_prefix=_ENV_PREFIX, emergency_keywords=EMERGENCY_KEYWORDS)
_consent_service = ConsentService()
_lead_service = LeadService(get_db_fn=_get_db, env_prefix=_ENV_PREFIX, consent=_consent_service, push_fn=push, commit_fn=commitSignificantActivity)
_unknown_question_service = UnknownQuestionService(consent=_consent_service, push_fn=push, commit_fn=commitSignificantActivity)
_about_service = AboutService(me_context=_ME, trim_fn=PromptSystemMaker.trim)

_debug_logger = DebugLogger(get_db_fn=_get_db, env_prefix=_ENV_PREFIX, consent_service=_consent_service)

# ToolRouter — F-05 fix
_tool_router = ToolRouter()
_tool_router.register_with_models([
    ("find_providers",          _find_care.search_providers,            ProviderSearchInput),
    ("find_specialty_codes",    _find_care.identify_specialty,          SpecialtyInput),
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
    return _tool_router.handle_tool_calls(tool_use_blocks, messages, _format_chat_history)

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
app = FastAPI(title="ChatHealthy FindCare API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chathealthy.ai", "https://www.chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"http://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    allow_credentials=False, allow_methods=["*"], allow_headers=["*"],
)

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []

class PaginationMeta(BaseModel):
    has_more: bool = False
    first_npi: Optional[str] = None
    last_npi: Optional[str] = None
    count: int = 0
    total_count: int = 0
    page_start: int = 1
    page_end: int = 0
    search_params: Optional[dict] = None

class ChatResponse(BaseModel):
    response: Optional[str] = None
    emergency: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    pagination: Optional[PaginationMeta] = None

class SearchRequest(BaseModel):
    """Direct provider search — bypasses Claude. Used for pagination."""
    specialty_query: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    name: Optional[str] = None
    npi: Optional[str] = None
    specialty_codes: Optional[list[str]] = None
    after_npi: Optional[str] = None
    limit: int = 25

@app.post("/search")
async def search(body: SearchRequest):
    """Direct provider search — for pagination. No LLM involved."""
    params = body.model_dump(exclude_none=True)
    result = _find_care.search_providers(**params)
    return result

@app.get("/welcome")
def welcome():
    return {"message": _build_test_welcome() if _HUMAN_TESTING else WELCOME_MESSAGE}

@app.get("/health")
def health():
    env_label = _ENV_PREFIX if os.getenv("SPACE_ID") else "local"
    return {"status": "ok", "db": "connected" if _get_db() else "unavailable",
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
        _debug_logger.log_chat(ip, body.message, len(body.history), 0, None, None, None, err_msg, body.history)
        return ChatResponse(error=err_msg, error_type=err_type)

# ---------------------------------------------------------------------------
# Chat loop
# ---------------------------------------------------------------------------
async def _chat_inner(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")

    if _safety_service.try_admin_unlock(body.message, ip):
        return ChatResponse(response="Session unlocked.")
    if _safety_service.is_ip_locked(ip):
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)
    if _safety_service.is_emergency(body.message):
        full_history = list(body.history) + [{"role": "user", "content": body.message}]
        _safety_service.lock_ip(ip, trigger_message=body.message, history=full_history)
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    user_msg_count = sum(1 for m in body.history if m.get("role") == "user")
    system = _system_prompt(follow_up_check=user_msg_count > 0 and user_msg_count % 5 == 0)
    messages = list(body.history) + [{"role": "user", "content": body.message}]
    client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    total_in = total_out = 0

    _log.info("CHAT call=initial msgs=%d", len(messages))
    response = client.messages.create(
        model="claude-sonnet-4-6", max_tokens=4096, system=system, messages=messages, tools=anthropic_tools)
    total_in  += getattr(response.usage, "input_tokens", 0)
    total_out += getattr(response.usage, "output_tokens", 0)

    loop_iter = 0
    last_provider_result = None  # capture pagination metadata from find_providers

    while response.stop_reason == "tool_use":
        tool_uses = [b for b in response.content if b.type == "tool_use"]
        tool_results = _handle_tool_calls(tool_uses, messages)

        # Capture the last find_providers result for pagination
        for i, block in enumerate(tool_uses):
            if block.name == "find_providers":
                try:
                    last_provider_result = json.loads(tool_results[i]["content"])
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": tool_results})
        loop_iter += 1
        _log.info("CHAT tool_loop iter=%d tools=%s", loop_iter, [b.name for b in tool_uses])
        response = client.messages.create(
            model="claude-sonnet-4-6", max_tokens=4096, system=system, messages=messages, tools=anthropic_tools)
        total_in  += getattr(response.usage, "input_tokens", 0)
        total_out += getattr(response.usage, "output_tokens", 0)

    text = next((b.text for b in response.content if b.type == "text"), "")
    text = _url_guardian.guard_text(text)
    text = re.sub(r'(?<!\[)Skip Snow on LinkedIn(?!\])',
                  '[Skip Snow on LinkedIn](https://linkedin.com/in/skipsnow)', text)

    # Build pagination metadata if find_providers returned results
    pagination = None
    if last_provider_result and isinstance(last_provider_result, dict):
        total_count = last_provider_result.get("total_count", 0)
        if total_count > 0:
            pagination = PaginationMeta(
                has_more=last_provider_result.get("has_more", False),
                first_npi=last_provider_result.get("first_npi"),
                last_npi=last_provider_result.get("last_npi"),
                count=last_provider_result.get("count", 0),
                total_count=total_count,
                page_start=last_provider_result.get("page_start", 1),
                page_end=last_provider_result.get("page_end", 0),
                search_params=last_provider_result.get("search_params"),
            )

    _log.info("CHAT complete tokens_in=%d tokens_out=%d pagination=%s", total_in, total_out, bool(pagination))
    _debug_logger.log_chat(ip, body.message, len(body.history), loop_iter, total_in, total_out, text, None)
    return ChatResponse(response=text, tokens_in=total_in, tokens_out=total_out, pagination=pagination)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    from starlette.staticfiles import StaticFiles
    from starlette.responses import FileResponse

    @app.get("/")
    async def serve_index():
        return FileResponse(
            os.path.join(_static_dir, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "7860")))
