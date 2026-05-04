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
    """Send an operator-notification email via SparkPost.

    Returns:
        {"sent": True}                 — delivered
        {"sent": False, "skipped": ...} — env var missing, intentionally skipped
        {"sent": False, "error": ...}  — SparkPost call raised; caller must
                                         see this and decide what to do
                                         (no silent swallow per the no-fallback
                                         rule).
    """
    if not _SPARKMAIL_API_KEY:
        return {"sent": False, "skipped": "SPARKMAIL_API_KEY not configured"}
    try:
        from sparkpost import SparkPost
        SparkPost(_SPARKMAIL_API_KEY).transmissions.send(
            recipients=[_SPARKMAIL_TO], from_email=_SPARKMAIL_FROM,
            subject="ChatHealthy — Activity", text=message,
        )
        return {"sent": True}
    except Exception as exc:
        _log.warning("SparkPost send failed: %s", exc)
        return {"sent": False, "error": f"{type(exc).__name__}: {exc}"}

def commitSignificantActivity(payload=None, **kwargs):
    """Commit a significant-activity record to MongoDB.

    Failure semantics (NO silent fallbacks):
      - DB unavailable         → {"recorded": "skipped", "reason": "db_unavailable"}
                                 (NOT "ok" — a skipped commit is not a successful one)
      - Bad payload / DB error → {"recorded": "error", "error": "..."} — caller
                                 MUST inspect this; upstream code has no excuse
                                 for treating this as success.
    """
    if _get_db() is None:
        return {"recorded": "skipped", "reason": "db_unavailable"}
    try:
        payload = payload or kwargs
        if isinstance(payload, str):
            payload = json.loads(payload)
        return _db_manager.commit(_ENV_PREFIX, payload["database"], payload["collection"], payload["record"])
    except Exception as exc:
        _log.error("commitSignificantActivity failed: %s", exc)
        return {"recorded": "error", "error": f"{type(exc).__name__}: {exc}"}

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
# Build/version/framework: live from MongoDB per EPIC-008-F-004-S-001-REQ-T-002

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

# EPIC-008-F-011-S-001-REQ-B-002 / REQ-B-003 — uniform fatal-error contract.
# EPIC-003: consumed from the chathealthy-frontend-lib package.
from chathealthy_frontend_lib.runtime_governance import ChatHealthyFatalError, register_fatal_handler
register_fatal_handler(app, service_name="FindCare")

# ── EPIC-002-F-001-S-012-REQ-T-004: startup security-primitive verification ──
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
    startup check turns into an exit-78 abend per EPIC-002-F-001-S-012-REQ-T-004.
    """
    import base64
    runtime_dir = "/tmp/ch_certs"
    mapping = {
        "FINDCARE_SIGNING_KEY_PEM":  "findcare.key",
        "FINDCARE_CERT_PEM":         "findcare.crt",
        # SEC-HTTPS-001-REQ-021: FindCare verifies tokens minted by peers
        # (page-owning service mints; FindCare verifies for mutual auth).
        "SHARED_CERT_PEM":           "shared.crt",
        "EVALCARE_CERT_PEM":         "evalcare.crt",
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
    try:
        from chathealthy_frontend_lib.session_token import generate_session_token
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
    """EPIC-006-F-002-S-001-REQ-T-001: AI vector search for specialties.
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
    except Exception as exc:
        # NO silent swallow: surface the read failure to the caller via a
        # typed error field on the response. Caller can render a visible
        # warning rather than mistaking an empty list for "no homeopathic
        # generalists exist."
        _log.warning("classify: homeopathic_generalists Mongo read failed: %s", exc)
        homeo_generalists = []
        homeo_generalists_error = f"{type(exc).__name__}: {exc}"
    else:
        homeo_generalists_error = None

    response = {
        "specialties": specialties,
        "homeopathic_generalists": homeo_generalists,
        "state": state,
        "city": city,
        "county": county,
        "model": "text-embedding-3-large (vector search)",
    }
    if homeo_generalists_error is not None:
        response["homeopathic_generalists_error"] = homeo_generalists_error
    return response

@app.post("/welcome")
def welcome():
    return {"message": WELCOME_MESSAGE}

_REQUIRED_INDEXES = {
    "providers": ["provider_vector_index"],
    "SpecialtyMetaData": ["specialty_vector_index"],
}

def _check_indexes() -> dict:
    """DR-016/DR-018: verify all required vector search indexes exist.

    Failure semantics (NO silent fallbacks):
      - DB unreachable           → status: "db_unavailable" (caller must
                                   degrade /health, not call this "ok")
      - Index list call raises   → status: "fail" with errors[] explaining
                                   which collections couldn't be checked.
                                   NOT silently appending "/ERROR" to
                                   missing[] (which conflated unreadable
                                   with absent).
      - Indexes legitimately
        missing                  → status: "fail" with missing[] populated.
    """
    db = _get_db()
    if db is None:
        return {"status": "db_unavailable", "missing": [], "errors": []}
    missing = []
    errors = []
    for coll_name, index_names in _REQUIRED_INDEXES.items():
        try:
            existing = [idx.get("name") for idx in
                        db[f"{_ENV_PREFIX}_PublicHealthData"][coll_name].list_search_indexes()]
        except Exception as exc:
            errors.append({"collection": coll_name, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for idx in index_names:
            if idx not in existing:
                missing.append(f"{coll_name}/{idx}")
    status = "ok" if not missing and not errors else "fail"
    return {"status": status, "missing": missing, "errors": errors}

# graph-exempt: health check — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
@app.post("/health")
def health():
    """Health-state report. Returns 200 always — the body's `status` field
    carries the result. /health is a state report, not a fatal-trigger:
    operators need it to RESPOND when Mongo is down so monitoring can see
    the degraded state. NO silent-fallback `?` placeholders — explicit
    None when the read didn't succeed."""
    env_label = _ENV_PREFIX if os.getenv("SPACE_ID") else "local"
    idx_check = _check_indexes()
    _build = None
    _version_str = None
    _framework_str = None
    _version_error = None
    db = _get_db()
    if db is not None:
        try:
            doc = db["admin"]["Versions"].find_one(sort=[("from", -1)]) or {}
            _build = doc.get("build")
            _version_str = doc.get("version")
            _framework_str = doc.get("framework")
        except Exception as _exc:
            _log.warning("/health: MongoDB read for build/version/framework failed: %s", _exc)
            _version_error = f"{type(_exc).__name__}: {_exc}"
    db_status = "connected" if db is not None and _version_error is None else (
        "unavailable" if db is None else "unreachable")
    status = "ok" if (idx_check["status"] == "ok" and db_status == "connected") else "degraded"
    result = {"status": status, "db": db_status,
              "env": env_label,
              "build": _build,
              "version": _version_str,
              "framework": _framework_str}
    if idx_check.get("missing"):
        result["missing_indexes"] = idx_check["missing"]
        _log.error("HEALTH CHECK: missing indexes — %s", idx_check["missing"])
    if _version_error:
        result["version_error"] = _version_error
    return result

# ── Session token — signed with FindCare's x509 cert ──────────
# graph-exempt: session token read, no LLM — security primitive; per BUG-ARCH-GRAPH-EXEMPT-001
@app.post("/session")
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
    from chathealthy_frontend_lib.session_token import generate_session_token
    token = generate_session_token("FindCare")
    # EPIC-002-F-001-S-012-REQ-B-010: server-asserted env (FindCare's deployment env).
    # `origin` field stays "FindCare" for cryptographic signature verification
    # (REQ-017); this added `server_env` carries the display-semantic value.
    token["server_env"] = _ENV_PREFIX
    return token

# Body model for /verify-token (callers POST {session_token: {...}}).
class EvaluateRequest(BaseModel):
    providers: list[dict] = []
    session_token: Optional[dict] = None
    question_summary: str = ""

# SEC-HTTPS-001-REQ-021: FindCare verifies tokens minted by peer services.
# When SharedServices or EvaluateCare owns the page, the browser fetches
# /session from THAT service (so the token is signed with the page-owning
# service's key), then sends the signed token here for FindCare to verify
# with the corresponding peer cert. Acts as the independent third-party
# verifier — preserves mutual authentication when the minter is a peer.
# graph-exempt: mTLS/session security primitive, no LLM; per BUG-ARCH-GRAPH-EXEMPT-001
@app.post("/verify-token")
def verify_token(body: EvaluateRequest):
    """Verify a session token signed by FindCare, SharedServices, or EvaluateCare.

    The expected origin is read from the token itself; the cert lookup in
    session_token resolves `{origin.lower()}.crt` from CERTS_DIR. If the
    signature doesn't match the cert for the claimed origin, verification
    fails — so a caller can't lie about origin.
    """
    if not body.session_token:
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail="session_token is required")
    from chathealthy_frontend_lib.session_token import verify_session_token
    token_origin = body.session_token.get("origin", "unknown")
    try:
        token_valid = verify_session_token(body.session_token, token_origin)
    except ValueError as e:
        _log.warning("FindCare verify-token 400: %s", e)
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=str(e)) from e
    _log.info("FindCare verify-token: origin=%s valid=%s",
              token_origin, token_valid)
    return {
        "status": "verified" if token_valid else "failed",
        "session_token": {
            "token_received": body.session_token.get("token", ""),
            "signature_received": (body.session_token.get("signature", "") or "")[:40] + "..." if body.session_token.get("signature") else "none",
            # EPIC-002-F-001-S-012-REQ-B-010: origin field is the name of the
            # RESPONDING service (self-identification). Distinct from
            # the cryptographic signer (REQ-017, always FindCare).
            "origin": "FindCare",
            "verified": token_valid,
            "created_at": body.session_token.get("created_at", "") or "",
            "server_env": body.session_token.get("server_env"),
        },
    }

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
        # EPIC-008-F-011-S-001-REQ-B-001: GPT-extraction failure is not declared
        # less-than-fatal. Surface as fatal rather than silently feeding the
        # user's raw message into the search pipeline as if extraction succeeded.
        raise ChatHealthyFatalError(
            api="/chat (term extraction)",
            source="openai chat.completions",
        ) from exc


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
        # EPIC-008-F-011-S-001-REQ-B-001: GOV-011-STD-001 dedup failure is not
        # declared less-than-fatal. Don't silently return un-cleaned text.
        raise ChatHealthyFatalError(
            api="/chat (summary dedup)",
            source="openai chat.completions",
        ) from exc


# ---------------------------------------------------------------------------
# Chat handler — sequential implementation (LangGraph removed 2026-04-20)
# ---------------------------------------------------------------------------
import sys as _sys
_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared"))
from llm_client import chat as llm_chat

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

    _CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4.1")

    user_msg_count = sum(1 for m in body.history if m.get("role") == "user")
    system = _system_prompt(follow_up_check=user_msg_count > 0 and user_msg_count % 5 == 0)
    messages = list(body.history) + [{"role": "user", "content": body.message}]
    total_in = total_out = 0

    _log.info("CHAT model=%s call=initial msgs=%d", _CHAT_MODEL, len(messages))
    response = llm_chat(_CHAT_MODEL, messages, tools=anthropic_tools, system=system, max_tokens=4096)
    total_in  += response.get("usage", {}).get("input_tokens", 0)
    total_out += response.get("usage", {}).get("output_tokens", 0)

    loop_iter = 0
    last_provider_result = None
    last_trials_result = None

    while response.get("stop_reason") in ("tool_calls", "tool_use"):
        tool_calls = response.get("tool_calls", [])
        if not tool_calls:
            break
        tool_results = _tool_router.handle_normalized_tool_calls(tool_calls, messages, _format_chat_history)

        for i, tc in enumerate(tool_calls):
            tc_name = tc["function"]["name"]
            if tc_name == "find_providers":
                try:
                    last_provider_result = json.loads(tool_results[i]["content"])
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass
            elif tc_name == "search_clinical_trials":
                try:
                    last_trials_result = json.loads(tool_results[i]["content"])
                except (json.JSONDecodeError, KeyError, IndexError):
                    pass

        assistant_msg = {"role": "assistant", "content": response.get("content", "")}
        if tool_calls:
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": "function", "function": tc["function"]}
                for tc in tool_calls
            ]
        messages.append(assistant_msg)
        for tr in tool_results:
            messages.append(tr)

        loop_iter += 1
        _log.info("CHAT tool_loop iter=%d tools=%s", loop_iter, [tc["function"]["name"] for tc in tool_calls])
        response = llm_chat(_CHAT_MODEL, messages, tools=anthropic_tools, system=system, max_tokens=4096)
        total_in  += response.get("usage", {}).get("input_tokens", 0)
        total_out += response.get("usage", {}).get("output_tokens", 0)

    text = response.get("content", "")
    text = _url_guardian.guard_text(text)
    text = re.sub(r'(?<!\[)Skip Snow on LinkedIn(?!\])',
                  '[Skip Snow on LinkedIn](https://linkedin.com/in/skipsnow)', text)

    pagination = None
    if last_provider_result and isinstance(last_provider_result, dict):
        total_count = last_provider_result.get("total_count", 0)
        if total_count > 0:
            from domain.find_care.provider_search_service import FindCareService
            user_term = _extract_user_search_term(body.message)
            user_summary = FindCareService._build_summary_message(
                has_more=last_provider_result.get("has_more", False),
                total_count=total_count,
                page_count=last_provider_result.get("count", 0),
                specialty_searched=user_term,
                specialization_options=last_provider_result.get("specialization_options"),
                state=last_provider_result.get("state", ""),
                city=(last_provider_result.get("search_params") or {}).get("city", ""),
                county=(last_provider_result.get("search_params") or {}).get("county", ""),
            )
            pagination = PaginationMeta(
                has_more=last_provider_result.get("has_more", False),
                first_npi=last_provider_result.get("first_npi"),
                last_npi=last_provider_result.get("last_npi"),
                count=last_provider_result.get("count", 0),
                total_count=total_count,
                page_start=last_provider_result.get("page_start", 1),
                page_end=last_provider_result.get("page_end", 0),
                search_params=last_provider_result.get("search_params"),
                specialization_options=last_provider_result.get("specialization_options"),
                summary_message=user_summary,
            )

    trials_meta = None
    if last_trials_result and isinstance(last_trials_result, dict):
        trial_list = last_trials_result.get("trials", [])
        if trial_list:
            trial_count = len(trial_list)
            has_travel = any(t.get("travel_info") for t in trial_list)
            user_msg = _extract_user_search_term(body.message)
            parts = [f"Found {trial_count} recruiting trial{'s' if trial_count != 1 else ''} for '{user_msg}'."]
            if has_travel:
                parts.append(" Travel times included.")
            parts.append(f" [View all on ClinicalTrials.gov](https://clinicaltrials.gov/search?cond={_requests_lib.utils.quote(user_msg)}&aggFilters=status:rec)")
            trials_meta = TrialsMeta(
                trial_count=trial_count,
                condition=user_msg,
                summary_message="".join(parts),
            )

    if pagination and pagination.summary_message and text:
        text = _strip_redundant_summary(text, pagination.total_count, pagination.count)

    _log.info("CHAT complete tokens_in=%d tokens_out=%d pagination=%s trials=%s",
              total_in, total_out, bool(pagination), bool(trials_meta))
    _debug_logger.log_chat(ip, body.message, len(body.history), loop_iter, total_in, total_out, text, None)
    return ChatResponse(response=text, tokens_in=total_in, tokens_out=total_out,
                        pagination=pagination, trials=trials_meta)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    from starlette.staticfiles import StaticFiles
    from starlette.responses import FileResponse

    # graph-exempt: static page render — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
    @app.get("/")
    async def serve_index():
        return FileResponse(
            os.path.join(_static_dir, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

    app.mount("/assets", StaticFiles(directory=os.path.join(_static_dir, "assets")), name="assets")

if __name__ == "__main__":
    import uvicorn
    kwargs = {"host": "0.0.0.0", "port": int(os.getenv("PORT", "7860"))}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
