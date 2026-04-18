# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
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
import requests as _requests_lib
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

def _get_db():
    global _db_manager
    if not _mongo_frontend_str:
        return None
    try:
        if _db_manager is None:
            _db_manager = ChatHealthyMongoUtilities(_mongo_frontend_str)
        return _db_manager.getConnection()
    except Exception as e:
        _log.warning("MongoDB unavailable (will retry next call): %s", e)
        _db_manager = None
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
# Build number comes from version.json only — single source of truth

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
    get_vector_fn=_embedding_client.get_specialty_vector)
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
import time as _time_mod

app = FastAPI(title="ChatHealthy FindCare API")

# ── SEC-HTTPS-001-REQ-014: startup security-primitive verification ──
# /session depends on session_token.generate_session_token producing a
# well-formed (68-char, "CH"-prefixed) token. If the primitive cannot
# initialize here, serving MUST NOT continue. Exit codes per sysexits.h:
#   78 (EX_CONFIG)    — missing library, missing config, malformed output
#   77 (EX_NOPERM)    — permission denied on cert/key
#   70 (EX_SOFTWARE)  — unexpected internal error
# The underscore check guards against regression to the old silent-fallback
# placeholder CH_{uuid} (BUG-SEC-007).
def _bootstrap_certs_from_env():
    """Write PKI material from env vars to a runtime dir and point CERTS_DIR at it.

    HF Spaces don't support bind-mounted cert directories. The deploy pipeline
    stores the signing key and public cert as HF Space secrets (base64-encoded
    PEM). On startup we decode them to /tmp/ch_certs and set CERTS_DIR.

    If none of the env vars are present (e.g. local dev, local Docker with a
    bind-mounted /certs), the function is a no-op — the caller's CERTS_DIR
    resolution remains in effect. Malformed PEM content raises, which the
    startup check turns into an exit-78 abend per SEC-HTTPS-001-REQ-014.
    """
    import base64
    runtime_dir = "/tmp/ch_certs"
    mapping = {
        "FINDCARE_SIGNING_KEY_PEM":  "findcare.key",
        "FINDCARE_CERT_PEM":         "findcare.crt",
        "CA_CERT_PEM":               "ca.crt",
    }
    wrote = []
    for env_var, filename in mapping.items():
        b64 = os.environ.get(env_var)
        if not b64:
            continue
        try:
            pem_bytes = base64.b64decode(b64.strip())
        except Exception as _exc:
            _log.error("STARTUP: %s is present but not valid base64: %s", env_var, _exc)
            raise
        os.makedirs(runtime_dir, exist_ok=True)
        path = os.path.join(runtime_dir, filename)
        with open(path, "wb") as f:
            f.write(pem_bytes)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        wrote.append(filename)
    if wrote:
        os.environ["CERTS_DIR"] = runtime_dir
        _log.info("startup bootstrap: wrote %s to %s (CERTS_DIR=%s)",
                  ",".join(wrote), runtime_dir, runtime_dir)

def _startup_security_verification():
    import sys as _sys
    _bootstrap_certs_from_env()
    _local_shared = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared")
    if _local_shared not in _sys.path:
        _sys.path.insert(0, _local_shared)
    os.environ.setdefault("CERTS_DIR", os.path.join(_local_shared, "ops", "certs"))
    try:
        from session_token import generate_session_token
    except ImportError as _imp:
        _log.error("STARTUP ABEND exit=78 primitive=session_token reason=import_failed: %s", _imp)
        _sys.exit(78)
    except PermissionError as _perm:
        _log.error("STARTUP ABEND exit=77 primitive=session_token reason=permission: %s", _perm)
        _sys.exit(77)
    try:
        _probe = generate_session_token("FindCare")
    except FileNotFoundError as _fnf:
        _log.error("STARTUP ABEND exit=78 primitive=session_token reason=missing_key: %s", _fnf)
        _sys.exit(78)
    except PermissionError as _perm:
        _log.error("STARTUP ABEND exit=77 primitive=session_token reason=permission_on_key: %s", _perm)
        _sys.exit(77)
    except Exception as _exc:
        _log.error("STARTUP ABEND exit=70 primitive=session_token reason=generator_raised: %s", _exc)
        _sys.exit(70)
    _tok = (_probe or {}).get("token", "")
    if not _tok.startswith("CH") or len(_tok) < 68 or "_" in _tok:
        _log.error("STARTUP ABEND exit=78 primitive=session_token reason=malformed_token: %r", _probe)
        _sys.exit(78)
    _log.info("startup security check PASSED — session_token OK (len=%d signed=%s)",
              len(_tok), (_probe or {}).get("signed"))

_startup_security_verification()

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = _time_mod.time()
    response = await call_next(request)
    elapsed = round((_time_mod.time() - start) * 1000)
    _log.info("REQUEST %s %s → %d (%dms) from %s",
              request.method, request.url.path, response.status_code, elapsed,
              request.headers.get("x-forwarded-for", request.client.host if request.client else "unknown"))
    return response

app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://chathealthy.ai", "https://www.chathealthy.ai", "https://dev.chathealthy.ai"],
    allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
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
    specialization_options: Optional[list[dict]] = None
    summary_message: Optional[str] = None

class TrialsMeta(BaseModel):
    trial_count: int = 0
    condition: str = ""
    location: str = ""
    summary_message: Optional[str] = None

class ChatResponse(BaseModel):
    response: Optional[str] = None
    emergency: bool = False
    error: Optional[str] = None
    error_type: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    pagination: Optional[PaginationMeta] = None
    trials: Optional[TrialsMeta] = None

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


class ClassifyRequest(BaseModel):
    """GOV-011: AI translates the user's question into structured search parameters.
    One AI call. System answers with DB query after."""
    message: str

@app.post("/classify")
async def classify(body: ClassifyRequest, request: Request):
    """FC-FILT-001-REQ-001: AI vector search for specialties.
    Replaces the GPT classify call with embedding + vector search.
    Returns specialty codes ranked by cosine similarity + location extracted by simple parsing."""

    # BUG-SAFETY-001: Safety gate — emergency detection must run on every user input,
    # regardless of which endpoint the frontend calls.
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")
    if _safety_service.is_ip_locked(ip):
        return {"emergency": True, "response": EMERGENCY_RESPONSE}
    if _safety_service.is_emergency(body.message):
        full_history = [{"role": "user", "content": body.message}]
        _safety_service.lock_ip(ip, trigger_message=body.message, history=full_history)
        return {"emergency": True, "response": EMERGENCY_RESPONSE}

    # Vector search for specialties
    result = _specialty_service.find_specialty_codes(body.message)

    if "error" in result:
        return {"specialties": [], "error": result["error"]}

    # Map to the format the frontend expects
    specialties = [
        {"code": s["Code"], "name": s["Display Name"],
         "can_prescribe": s.get("can_prescribe", False),
         "homeopathic": s.get("homeopathic", False),
         "rank": s.get("rank", 0)}
        for s in result.get("specialties", [])
    ]

    # Simple location extraction — no LLM needed
    msg = body.message.lower()
    state = None
    city = None
    county = None
    # State codes
    for code in ["DE", "MS", "VA", "IN"]:
        if f" {code.lower()} " in f" {msg} " or msg.endswith(f" {code.lower()}"):
            state = code
            break
    # Full state names
    state_names = {"delaware": "DE", "mississippi": "MS", "virginia": "VA", "indiana": "IN"}
    for name, code in state_names.items():
        if name in msg:
            state = code
            break

    # Load homeopathic generalists for client-side cache
    homeo_generalists = []
    try:
        db = _get_db()
        if db:
            for doc in db[f"{_ENV_PREFIX}_PublicHealthData"]["SpecialtyMetaData"].find(
                {"homeopathic_general": True, "Section": "Individual"},
                {"_id": 0, "Code": 1, "Display Name": 1, "can_prescribe": 1, "homeopathic": 1}
            ):
                homeo_generalists.append({
                    "code": doc.get("Code", ""),
                    "name": doc.get("Display Name", ""),
                    "can_prescribe": doc.get("can_prescribe", False),
                    "homeopathic": True,
                    "homeopathic_general": True,
                })
    except Exception:
        pass

    return {
        "specialties": specialties,
        "homeopathic_generalists": homeo_generalists,
        "state": state,
        "city": city,
        "county": county,
        "model": "text-embedding-3-large (vector search)",
    }

def _get_qa_report():
    """Load QA report from MongoDB (source of truth), fall back to file for bootstrap."""
    db = _get_db()
    if db:
        doc = db[f"{_ENV_PREFIX}_System"]["bugs"].find_one(
            {"_record_id": "qa_report_v014_sit"}, {"_id": 0})
        if doc:
            return doc
    # Bootstrap: load from file and seed into MongoDB
    report_path = os.path.join(_brain_dir, "machine_artifacts", "content", "qa_report_v014_sit.json")
    if not os.path.exists(report_path):
        report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain_data", "qa_report_v014_sit.json")
    if not os.path.exists(report_path):
        return None
    with open(report_path) as f:
        report = json.load(f)
    if db:
        db[f"{_ENV_PREFIX}_System"]["bugs"].replace_one(
            {"_record_id": "qa_report_v014_sit"}, report, upsert=True)
        _log.info("QA report seeded to MongoDB from file")
    return report

@app.get("/qa-report")
def qa_report():
    """DEVOPS-QA-001: Render QA report from MongoDB. Dev/QA only."""
    if _ENV_PREFIX == "prod":
        return {"error": "QA report not available in production"}
    report = _get_qa_report()
    if not report:
        return {"error": "QA report not found"}
    features = report.get("features", [])
    # Build HTML with editable dropdowns (DEVOPS-QA-005)
    options = "".join(f'<option value="{s}">{s}</option>' for s in
                      ["", "PASS", "FAIL", "DEFERRED", "NOT_STARTED", "IN_PROGRESS", "TO_TEST", "UNTESTED", "RELEASE_BLOCKER"])
    rows = ""
    for feat in features:
        status = feat.get("status", "NOT_STARTED")
        color = {"PASS": "#059669", "IN_PROGRESS": "#2563eb", "NOT_STARTED": "#9ca3af",
                 "UNTESTED": "#d97706", "RELEASE_BLOCKER": "#dc2626", "TO_TEST": "#7c3aed"}.get(status, "#6b7280")
        tc_count = len(feat.get("test_cases", []))
        tc_pass = sum(1 for tc in feat.get("test_cases", []) if tc.get("status") == "PASS")
        rows += f'<tr style="background:#f9fafb"><td>{feat.get("id","")}</td><td><b>{feat.get("feature_id","")}</b></td>'
        rows += f'<td><b>{feat.get("name","")}</b></td><td>{feat.get("epic","")}</td>'
        rows += f'<td style="color:{color};font-weight:600">{status}</td>'
        rows += f'<td>{tc_pass}/{tc_count}</td></tr>\n'
        for tc in feat.get("test_cases", []):
            tc_id = tc.get("tc", "")
            tc_status = tc.get("status", "")
            sel_options = options.replace(f'value="{tc_status}"', f'value="{tc_status}" selected')
            rows += f'<tr style="font-size:12px"><td></td><td></td>'
            rows += f'<td style="padding-left:24px">{tc_id}: {tc.get("test","")}</td>'
            rows += f'<td></td><td><select name="{tc_id}" style="font-size:11px;padding:2px">{sel_options}</select></td><td></td></tr>\n'
    summary = report.get("summary", {})
    from starlette.responses import HTMLResponse
    html = f"""<!DOCTYPE html><html><head><title>QA Report — {report.get('version','')}</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px}}
table{{border-collapse:collapse;width:100%}}th,td{{border:1px solid #e5e7eb;padding:8px 12px;text-align:left}}
th{{background:#f3f4f6;font-size:13px}}tr:hover{{background:#f9fafb}}
h1{{font-size:24px}}h2{{font-size:16px;color:#6b7280}}
select{{border:1px solid #d1d5db;border-radius:4px}}
.submit-btn{{background:linear-gradient(180deg,#0b9a94,#0b7a75);color:#fff;border:none;padding:10px 24px;
border-radius:6px;font-size:14px;font-weight:600;cursor:pointer;margin-top:16px}}
.submit-btn:hover{{opacity:0.9}}</style></head><body>
<form method="POST" action="/qa-report">
<h1>QA Report — {report.get('version','')} Dev→SIT</h1>
<h2>{report.get('scope','')}</h2>
<p>Date: {report.get('date','')} | Target: {report.get('target','')} | Status: {report.get('status','')}</p>
<p><b>Features:</b> {summary.get('total_features',0)} | <b>Test Cases:</b> {summary.get('total_test_cases',0)} |
<b style="color:#059669">Pass:</b> {summary.get('pass',0)} |
<b style="color:#2563eb">In Progress:</b> {summary.get('in_progress',0)} |
<b style="color:#9ca3af">Not Started:</b> {summary.get('not_started',0)}</p>
<table><tr><th>#</th><th>Feature ID</th><th>Name</th><th>Epic</th><th>Status</th><th>Tests</th></tr>
{rows}</table>
<button type="submit" class="submit-btn">Save QA Report</button>
</form>
<p style="font-size:11px;color:#9ca3af;margin-top:24px">&copy; 2026 Skip Snow. All rights reserved.</p>
</body></html>"""
    return HTMLResponse(content=html)

@app.post("/qa-report")
async def qa_report_submit(request: Request):
    """DEVOPS-QA-005: Save QA report edits to MongoDB."""
    if _ENV_PREFIX == "prod":
        return {"error": "QA report not available in production"}
    report = _get_qa_report()
    if not report:
        return {"error": "QA report not found"}
    form = await request.form()
    updated = 0
    for feat in report.get("features", []):
        for tc in feat.get("test_cases", []):
            tc_id = tc.get("tc", "")
            if tc_id in form and form[tc_id]:
                tc["status"] = form[tc_id]
                updated += 1
    # Recompute summary
    all_tc = [tc for f in report["features"] for tc in f.get("test_cases", [])]
    report["summary"]["pass"] = sum(1 for tc in all_tc if tc.get("status") == "PASS")
    report["summary"]["in_progress"] = sum(1 for tc in all_tc if tc.get("status") == "IN_PROGRESS")
    report["summary"]["not_started"] = sum(1 for tc in all_tc if tc.get("status") in ("NOT_STARTED", ""))
    # Write to MongoDB
    db = _get_db()
    if db:
        db[f"{_ENV_PREFIX}_System"]["bugs"].replace_one(
            {"_record_id": "qa_report_v014_sit"}, report, upsert=True)
    _log.info("QA report saved to MongoDB: %d test cases updated", updated)
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/qa-report", status_code=303)

@app.get("/welcome")
def welcome():
    return {"message": WELCOME_MESSAGE}

_REQUIRED_INDEXES = {
    "providers": ["provider_vector_index"],
    "SpecialtyMetaData": ["specialty_vector_index"],
}

def _check_indexes() -> dict:
    """DR-016/DR-018: verify all required vector search indexes exist."""
    db = _get_db()
    if db is None:
        return {"status": "db_unavailable"}
    missing = []
    for coll_name, index_names in _REQUIRED_INDEXES.items():
        try:
            existing = [idx.get("name") for idx in
                        db[f"{_ENV_PREFIX}_PublicHealthData"][coll_name].list_search_indexes()]
            for idx in index_names:
                if idx not in existing:
                    missing.append(f"{coll_name}/{idx}")
        except Exception:
            missing.append(f"{coll_name}/ERROR")
    return {"missing": missing, "status": "fail" if missing else "ok"}

@app.get("/health")
def health():
    env_label = _ENV_PREFIX if os.getenv("SPACE_ID") else "local"
    idx_check = _check_indexes()
    status = "ok" if idx_check["status"] == "ok" else "degraded"
    # Version info from version.json for version/framework
    _version_info = {}
    try:
        import json as _json
        # Try repo path first, then HF flat path
        _vpath = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "brain", "machine_artifacts", "content", "version.json")
        if not os.path.exists(_vpath):
            _vpath = os.path.join(os.path.dirname(__file__), "brain", "machine_artifacts", "content", "version.json")
        if os.path.exists(_vpath):
            _version_info = _json.loads(open(_vpath, encoding="utf-8").read()).get("current", {})
    except Exception:
        pass
    # BUG-DEVOPS-001 fix: build number from MongoDB (authoritative source,
    # written by bump_build.py in CI). Fall back to version.json if DB unavailable.
    _build = _version_info.get("build", "?")
    try:
        db = _get_db()
        if db:
            counter = db[f"{_ENV_PREFIX}_System"]["build_counter"].find_one({"_id": "build"})
            if counter and "number" in counter:
                _build = counter["number"]
    except Exception:
        pass
    result = {"status": status, "db": "connected" if _get_db() else "unavailable",
              "env": env_label,
              "build": _build,
              "version": _version_info.get("version", "?"),
              "framework": _version_info.get("framework", "?")}
    if idx_check.get("missing"):
        result["missing_indexes"] = idx_check["missing"]
        _log.error("HEALTH CHECK: missing indexes — %s", idx_check["missing"])
    return result

# ── Session token — signed with FindCare's x509 cert ──────────
@app.get("/session")
def get_session():
    """Generate a signed session token for this browser session.
    Client stores it in memory, passes it on cross-component calls.

    NO FALLBACK. Session token generation is a security primitive — if it
    fails, the caller must NOT proceed with a placeholder. Any failure
    propagates to a 500 so the client sees the problem and halts instead
    of running with a token that has no nonce and no signature. See
    BUG-SEC-007 (dev tokens missing nonce) and feedback_no_security_fallbacks.
    """
    import sys as _sys
    # Local layout: Shared/ is three levels up from backend/.
    # HF layout: session_token.py is copied flat into the hf_space root,
    # which is already on the Python path, so the sys.path.insert below
    # is a no-op there and the import resolves from the root. Either way,
    # the import MUST succeed — if it doesn't, something is wrong and the
    # caller needs to know.
    _local_shared = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared")
    if _local_shared not in _sys.path:
        _sys.path.insert(0, _local_shared)
    os.environ.setdefault("CERTS_DIR", os.path.join(_local_shared, "ops", "certs"))
    from session_token import generate_session_token
    return generate_session_token("FindCare")

# ── Evaluate proxy — FindCare backend → EvaluateCare over mTLS ──
class EvaluateRequest(BaseModel):
    providers: list[dict] = []
    session_token: Optional[dict] = None
    question_summary: str = ""

@app.get("/evaluate/splash")
def evaluate_splash():
    """Proxy splash call to EvaluateCare service. Transfers page ownership to EvaluateCare."""
    import requests as _req
    evalcare_url = os.getenv("EVALCARE_URL", "https://localhost:8001")
    certs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared", "ops", "certs")
    _log.info("CONTROL TRANSFER: FindCare → EvaluateCare (user clicked Evaluate)")
    req_kwargs = {"timeout": 5}
    if not os.getenv("SPACE_ID"):
        findcare_crt = os.path.join(certs_dir, "findcare.crt")
        findcare_key = os.path.join(certs_dir, "findcare.key")
        ca_crt = os.path.join(certs_dir, "ca.crt")
        if os.path.exists(findcare_crt) and os.path.exists(findcare_key):
            req_kwargs["cert"] = (findcare_crt, findcare_key)
        if os.path.exists(ca_crt):
            req_kwargs["verify"] = ca_crt
    try:
        resp = _req.get(f"{evalcare_url}/splash", **req_kwargs)
        return resp.json()
    except Exception as e:
        _log.error("CONTROL TRANSFER FAILED: EvaluateCare unreachable at %s: %s", evalcare_url, e)
        return {"html": '<div style="text-align:center;padding:20px;"><div style="font-size:24px;font-weight:700;color:#1f2937;">EvaluateCare</div><div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">Service unavailable</div></div>'}

@app.post("/transfer/to-findcare")
def transfer_to_findcare():
    """EvaluateCare calls FindCare facade to return page ownership.
    Called when user interacts with filter while in EvaluateCare mode.
    Proxies through EvaluateCare first so both services log the transfer."""
    import requests as _req
    evalcare_url = os.getenv("EVALCARE_URL", "https://localhost:8001")
    certs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared", "ops", "certs")
    _log.info("CONTROL TRANSFER: FindCare has taken ownership of the page (from EvaluateCare)")
    req_kwargs = {"timeout": 5}
    if not os.getenv("SPACE_ID"):
        ca_crt = os.path.join(certs_dir, "ca.crt")
        findcare_crt = os.path.join(certs_dir, "findcare.crt")
        findcare_key = os.path.join(certs_dir, "findcare.key")
        if os.path.exists(findcare_crt) and os.path.exists(findcare_key):
            req_kwargs["cert"] = (findcare_crt, findcare_key)
        if os.path.exists(ca_crt):
            req_kwargs["verify"] = ca_crt
    try:
        _req.post(f"{evalcare_url}/transfer/to-findcare", **req_kwargs)
    except Exception:
        _log.warning("Could not notify EvaluateCare of transfer")
    return {"owner": "findcare", "splash": WELCOME_MESSAGE}

@app.post("/evaluate/providers")
def evaluate_proxy(body: EvaluateRequest):
    """Proxy evaluate call through FindCare → EvaluateCare.
    Uses mTLS: FindCare presents its client cert, verifies against CA.
    EVALCARE_URL defaults to Caddy mTLS port (:8081) locally, HF space in dev/prod."""
    import requests as _req

    # EVALCARE_URL must be set in environment. No HTTP default.
    # Local: HTTPS via Caddy mTLS :8081
    # HF: HTTPS via HF space URL (set as HF Secret)
    evalcare_url = os.getenv("EVALCARE_URL", "https://localhost:8081")
    certs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared", "ops", "certs")

    req_kwargs = {"timeout": 15}
    if not os.getenv("SPACE_ID"):
        findcare_crt = os.path.join(certs_dir, "findcare.crt")
        findcare_key = os.path.join(certs_dir, "findcare.key")
        ca_crt = os.path.join(certs_dir, "ca.crt")
        if os.path.exists(findcare_crt) and os.path.exists(findcare_key):
            req_kwargs["cert"] = (findcare_crt, findcare_key)
        if os.path.exists(ca_crt):
            req_kwargs["verify"] = ca_crt

    try:
        resp = _req.post(
            f"{evalcare_url}/evaluate/providers",
            json={
                "providers": body.providers,
                "session_token": body.session_token,
                "question_summary": body.question_summary,
            },
            **req_kwargs,
        )
        return resp.json()
    except Exception as e:
        _log.error("EvaluateCare proxy failed: %s", e)
        return {"status": "error", "error": str(e)}

# ── Shared Services proxy — FindCare backend → CHShared over mTLS ──
@app.get("/shared/splash")
def shared_splash():
    """Proxy splash call to SharedServices. Transfers page ownership."""
    import requests as _req
    shared_url = os.getenv("SHARED_SERVICES_URL", "https://localhost:8002")
    certs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared", "ops", "certs")
    _log.info("CONTROL TRANSFER: FindCare → SharedServices (user clicked Shared Services)")
    req_kwargs = {"timeout": 5}
    if not os.getenv("SPACE_ID"):
        findcare_crt = os.path.join(certs_dir, "findcare.crt")
        findcare_key = os.path.join(certs_dir, "findcare.key")
        ca_crt = os.path.join(certs_dir, "ca.crt")
        if os.path.exists(findcare_crt) and os.path.exists(findcare_key):
            req_kwargs["cert"] = (findcare_crt, findcare_key)
        if os.path.exists(ca_crt):
            req_kwargs["verify"] = ca_crt
    try:
        resp = _req.get(f"{shared_url}/splash", **req_kwargs)
        return resp.json()
    except Exception:
        _log.error("CONTROL TRANSFER FAILED: SharedServices unreachable at %s", shared_url)
        return {"html": '<div style="text-align:center;padding:20px;"><div style="font-size:24px;font-weight:700;color:#1f2937;">Shared Services</div><div style="font-size:16px;font-weight:600;color:#6b7280;margin-top:8px;">Service unavailable</div></div>'}

@app.post("/shared/verify-token")
def shared_verify_token(body: EvaluateRequest):
    """Proxy token verification to SharedServices over mTLS."""
    import requests as _req
    shared_url = os.getenv("SHARED_SERVICES_URL", "https://localhost:8002")
    certs_dir = os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared", "ops", "certs")
    req_kwargs = {"timeout": 10}
    if not os.getenv("SPACE_ID"):
        findcare_crt = os.path.join(certs_dir, "findcare.crt")
        findcare_key = os.path.join(certs_dir, "findcare.key")
        ca_crt = os.path.join(certs_dir, "ca.crt")
        if os.path.exists(findcare_crt) and os.path.exists(findcare_key):
            req_kwargs["cert"] = (findcare_crt, findcare_key)
        if os.path.exists(ca_crt):
            req_kwargs["verify"] = ca_crt
    try:
        resp = _req.post(f"{shared_url}/verify-token",
                         json={"session_token": body.session_token},
                         **req_kwargs)
        return resp.json()
    except Exception as e:
        _log.error("SharedServices verify-token failed: %s", e)
        return {"status": "error", "error": str(e)}

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
# GOV-011-STD-002: Extract user's search term from their message using small model.
# "find me a bone doc in VA" → "bone doc"
# "show me shrinks near Richmond" → "shrinks"
# ---------------------------------------------------------------------------
def _extract_user_search_term(user_message: str) -> str:
    """Use GPT-4.1-nano to extract the colloquial search term from user's message."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            max_tokens=50,
            messages=[
                {"role": "system", "content": "Extract ONLY the provider/specialty search term from the user message. Return the PLURAL form preserving the user's exact wording style. Keep possessives and colloquialisms intact. Examples: kid's doc -> kid's docs, bone doc -> bone docs, shrink -> shrinks, children's doctor -> children's doctors. Just the plural term, nothing else."},
                {"role": "user", "content": user_message},
            ],
        )
        term = resp.choices[0].message.content.strip().strip("'\"")
        _log.info("GOV-011-STD-002: '%s' → '%s'", user_message, term)
        return term if term else user_message
    except Exception as exc:
        _log.warning("Search term extraction failed: %s", exc)
        return user_message


# ---------------------------------------------------------------------------
# GOV-011-STD-001: Strip redundant summary/pagination language from LLM text
# when the system has already built a summary_message.
# ---------------------------------------------------------------------------
def _strip_redundant_summary(text: str, total_count: int, page_count: int) -> str:
    """GOV-011-STD-001: Use GPT-4.1-mini to strip LLM content that duplicates
    the system summary. Keep only the provider listing. Remove all summary,
    pagination, filter suggestions, and conversational fluff."""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            max_tokens=4096,
            messages=[
                {"role": "system", "content": (
                    "You are a content editor. The user will give you an AI response that contains "
                    "a provider listing mixed with summary/navigation text. "
                    "KEEP ONLY the provider listing (names, addresses, phones, NPIs). "
                    "REMOVE everything else: introductions ('Here are the first...'), "
                    "summaries ('There are N more...'), filter suggestions ('Which type...'), "
                    "emoji bullet lists of provider types, pagination offers ('Would you like to see more...'), "
                    "location narrowing offers ('Are you looking in a specific city...'), "
                    "and any other conversational text that is not a provider record. "
                    "Return ONLY the cleaned provider listing. Preserve markdown formatting."
                )},
                {"role": "user", "content": text},
            ],
        )
        cleaned = resp.choices[0].message.content.strip()
        _log.info("GOV-011-STD-001: stripped %d → %d chars", len(text), len(cleaned))
        return cleaned if cleaned else text
    except Exception as exc:
        _log.warning("De-dup failed, returning original: %s", exc)
        return text


# ---------------------------------------------------------------------------
# LangGraph chat orchestration — replaces the sequential chat loop
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared"))
from llm_client import chat as llm_chat
from domain.find_care.provider_search_service import FindCareService as _FindCareServiceRef

from application.chat_graph import build_graph

_CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4.1")

_chat_graph = build_graph(
    safety_service=_safety_service,
    url_guardian=_url_guardian,
    prompt_maker=_prompt_maker,
    tool_router=_tool_router,
    anthropic_tools=anthropic_tools,
    llm_chat_fn=llm_chat,
    extract_search_term_fn=_extract_user_search_term,
    strip_redundant_summary_fn=_strip_redundant_summary,
    build_summary_message_fn=_FindCareServiceRef._build_summary_message,
    format_chat_history_fn=_format_chat_history,
    debug_logger=_debug_logger,
    emergency_response=EMERGENCY_RESPONSE,
    chat_model=_CHAT_MODEL,
)

async def _chat_inner(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")

    result = _chat_graph.invoke({
        "user_message": body.message,
        "history": body.history,
        "ip": ip,
    })

    final = result.get("final_response", {})

    # Build typed response models from graph output
    pagination = None
    if final.get("pagination"):
        p = final["pagination"]
        pagination = PaginationMeta(**p)

    trials_meta = None
    if final.get("trials"):
        t = final["trials"]
        trials_meta = TrialsMeta(**t)

    return ChatResponse(
        response=final.get("response"),
        emergency=final.get("emergency", False),
        tokens_in=final.get("tokens_in"),
        tokens_out=final.get("tokens_out"),
        pagination=pagination,
        trials=trials_meta,
    )

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
