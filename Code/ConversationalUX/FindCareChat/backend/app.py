# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# main.py — ChatHealthy.ai FindCare backend. Host adapter only.
#
# ARCH-001: All business logic in domain/ services. All config in PromptSystemMaker.
# This file: FastAPI setup, service wiring, chat loop. Nothing else.

# Establish which component this process is and what the library will let it
# load, before any other library capability is imported. The finder installed
# here refuses a forbidden module at import, so a late import inside a
# function is caught the same as one at the top of a file -- which only holds
# if nothing has been imported ahead of this call.
from chathealthy_lib.permissions import initialize as _ch_permissions_init
_ch_permissions_init()

import asyncio
import json
from chathealthy_lib import ChatHealthyLoggingService
import os
import sys
import traceback
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests as requests_lib

# FindCare/ on sys.path so business-model tools (SpecialtyFilter,
# ProviderManagement) are importable. Must happen BEFORE the imports
# below that pull from those packages. Dockerfile COPYs FindCare/ into
# /app/FindCare so the same relative walk resolves in the container.

log = ChatHealthyLoggingService()


sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "FindCare"))

# ARCH-001 — domain services
from application.tool_router import ToolRouter
from application.facades.evaluate_care_facade import EvaluateCareFacade
from ProviderManagement.provider_search_service import FindCareService
from SpecialtyFilter.filter import SpecialtyFilter
from domain.evaluate_care_quality.clinical_trials_service import ClinicalTrialsService
from ProviderDetail.provider_detail_service import ProviderDetailService
from domain.shared.safety.safety_service import SafetyService
from domain.shared.content.about_service import AboutService
from ProviderManagement.provider_search_models import ProviderSearchInput, SpecialtyInput
from application.tool_models.clinical_trials_models import ClinicalTrialsInput, ProviderDetailInput
from infrastructure.embeddings.embedding_client import EmbeddingClient
from infrastructure.debug_logger import DebugLogger

load_dotenv(override=True)

# Shared utilities
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "Shared"))
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib.exceptions import ChatHealthyException
from prompt_system_maker import PromptSystemMaker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENV_PREFIX    = os.getenv("ENV_PREFIX", "dev")
DEBUG         = os.getenv("DEBUG", "false").lower() == "true"
HUMAN_TESTING_RAW = os.getenv("HUMAN_TESTING", "false")
HUMAN_TESTING = HUMAN_TESTING_RAW.lower() not in ("false", "0", "")
APP_VERSION   = os.getenv("APP_VERSION", "unknown")

EMERGENCY_RESPONSE = (
    "<b>Call 911 or go to the nearest emergency room immediately. Do not wait.</b>\n\n"
    "<b>This chat has been suspended.</b>"
)

# ---------------------------------------------------------------------------
# MongoDB
# ---------------------------------------------------------------------------
db_manager = None

def get_db():
    global db_manager
    try:
        if db_manager is None:
            db_manager = ChatHealthyMongoUtilities()
        return db_manager.getConnection("frontendUser", "ChatHealthyFrontEnd")
    except Exception as e:
        # Mode 1 (REQ-B-008): recoverable — caller's next call will retry
        # via the same lazy-init path. log.info + default if_not_debug_log
        # so this only emits in debug mode.
        log.info("MongoDB unavailable (will retry next call): %s", e, exc=ChatHealthyException(
                                                                          mode="mongo_unavailable",
                                                                          message=f"MongoDB unavailable (will retry next call): {e}",
                                                                          component="FindCareBackend",
                                                                          exception=e,
                                                                      ))
        db_manager = None
        return None

# ---------------------------------------------------------------------------
# Utilities — push notification + DB write
# ---------------------------------------------------------------------------
SPARKMAIL_API_KEY = os.getenv("SPARKMAIL_API_KEY", "")
SPARKMAIL_FROM    = os.getenv("NOTIFICATION_FROM_EMAIL", "")
SPARKMAIL_TO      = os.getenv("NOTIFICATION_TO_EMAIL", "")

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
    if not SPARKMAIL_API_KEY:
        return {"sent": False, "skipped": "SPARKMAIL_API_KEY not configured"}
    try:
        from sparkpost import SparkPost
        SparkPost(SPARKMAIL_API_KEY).transmissions.send(
            recipients=[SPARKMAIL_TO], from_email=SPARKMAIL_FROM,
            subject="ChatHealthy — Activity", text=message,
        )
        return {"sent": True}
    except Exception as exc:
        # Mode 1 (REQ-B-008): SparkPost push notification is best-effort;
        # caller proceeds with the operation regardless. log.info + default
        # debug-gated.
        log.info("SparkPost send failed: %s", exc, exc=ChatHealthyException(
                                                       mode="sparkpost_send_failed",
                                                       message=f"SparkPost send failed: {exc}",
                                                       component="FindCareBackend",
                                                       exception=exc,
                                                   ))
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
    client = get_db()
    if client is None:
        return {"recorded": "skipped", "reason": "db_unavailable"}
    try:
        payload = payload or kwargs
        if isinstance(payload, str):
            payload = json.loads(payload)
        db_name = f"{ENV_PREFIX}_{payload['database']}"
        coll = client[db_name][payload["collection"]]
        record = dict(payload["record"])
        record["record_number"] = coll.count_documents({}) + 1
        record["datetime"] = dt.datetime.now().isoformat()
        coll.insert_one(record)
        return {"recorded": "ok"}
    except Exception as exc:
        # Mode 2 (REQ-B-008): audit-trail write failed; we return an
        # error dict to the caller (no 503) but operator MUST know — the
        # audit trail is the regulatory artifact, not optional.
        log.error("commitSignificantActivity failed: %s", exc, exc=ChatHealthyException(
                                                                mode="commit_significant_activity_failed",
                                                                message=f"commitSignificantActivity failed: {exc}",
                                                                component="FindCareBackend",
                                                                exception=exc,
                                                            ), if_not_debug_log=True)
        return {"recorded": "error", "error": f"{type(exc).__name__}: {exc}"}

def format_chat_history(messages, truncate: bool = True):
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
brain_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "..", "brain")
if not os.path.isdir(brain_dir):
    brain_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brain")
prompt_maker = PromptSystemMaker(brain_dir=brain_dir, env_prefix=ENV_PREFIX)
EMERGENCY_KEYWORDS = prompt_maker.load_emergency_keywords()
anthropic_tools = prompt_maker.load_tool_definitions()
WELCOME_MESSAGE = PromptSystemMaker.build_welcome_message()
# Build/version/framework: live from MongoDB per EPIC-008-F-004-S-001

ME_DIR = os.getenv("ME_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "me")
if not os.path.isdir(ME_DIR):
    ME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "ChatHealthyWhoAmIChat", "me")
ME = prompt_maker.load_me_context(ME_DIR)

# UAT report
# UAT report: local repo path or HF flat layout
def system_prompt(follow_up_check: bool = False) -> str:
    return prompt_maker.build_system_prompt(emergency_response=EMERGENCY_RESPONSE, follow_up_check=follow_up_check)

# ---------------------------------------------------------------------------
# Service initialization — ARCH-001
# ---------------------------------------------------------------------------
embedding_client = EmbeddingClient()

specialty_service = SpecialtyFilter(
    get_db_fn=get_db, env_prefix=ENV_PREFIX,
    get_vector_fn=embedding_client.get_specialty_vector)
find_care = FindCareService(
    get_db_fn=get_db, env_prefix=ENV_PREFIX,
    get_embedding_fn=embedding_client.get_query_embedding, specialty_service=specialty_service)

clinical_trials_service = ClinicalTrialsService()
provider_detail_service = ProviderDetailService()
evaluate_care_facade = EvaluateCareFacade(
    clinical_trials=clinical_trials_service, provider_detail=provider_detail_service, find_care_facade=find_care)

safety_service = SafetyService(get_db_fn=get_db, env_prefix=ENV_PREFIX, emergency_keywords=EMERGENCY_KEYWORDS)
about_service = AboutService(me_context=ME, trim_fn=PromptSystemMaker.trim)

debug_logger = DebugLogger(get_db_fn=get_db, env_prefix=ENV_PREFIX)

# ToolRouter — F-05 fix
tool_router = ToolRouter()
tool_router.register_with_models([
    ("find_providers",          find_care.search_providers,            ProviderSearchInput),
    ("find_specialty_codes",    find_care.identify_specialty,          SpecialtyInput),
    ("search_clinical_trials",  evaluate_care_facade.search_clinical_trials,  ClinicalTrialsInput),
    ("lookup_provider_external", evaluate_care_facade.get_provider_details,   ProviderDetailInput),
    ("get_skip_snow_context",   about_service.get_skip_snow_context),
    ("get_chathealthy_context", about_service.get_chathealthy_context),
    ("commitSignificantActivity", commitSignificantActivity),
])
log.info("ToolRouter initialized: %s", tool_router.registered_tools)

def handle_tool_calls(tool_use_blocks, messages):
    return tool_router.handle_tool_calls(tool_use_blocks, messages, format_chat_history)

# ---------------------------------------------------------------------------
# FastAPI
# ---------------------------------------------------------------------------
import time as time_mod

app = FastAPI(title="ChatHealthy FindCare API")


@app.exception_handler(ChatHealthyException)
async def _chathealthy_exception_to_response(request, exc: ChatHealthyException):
    """Return the response the raise site asked for.

    Raising ChatHealthyException instead of HTTPException moves the status
    code into the exception's context. This turns it back into the same
    response the client used to receive: same code, same detail body. Any
    other mode is an unhandled fault and answers 500.
    """
    # The boundary logs. Throwers were stripped of their log calls because
    # the rule says the catcher logs, and this is the catcher: without this
    # line a converted failure reaches the client as a status code and
    # leaves no trace anywhere of what happened.
    status = (int(exc.context.get("status_code", 500))
              if exc.mode == "http_error" else 500)
    # exc= takes a constructed ChatHealthyException or one bound by an
    # except clause; a parameter annotated as one is neither, so the facts
    # go in the line itself rather than bending the rule to fit this frame.
    log.error("%s %s -> %s  mode=%s component=%s  %s",
              request.method, request.url.path, status,
              exc.mode, exc.component or "-", exc.message)
    return JSONResponse(status_code=status, content={"detail": exc.message})

from chathealthy_lib.runtime_data_collections import (
    providers_coll,
    specialty_meta_coll,
    bind_from_manifest as bind_data_collections,
    router as data_collections_router,
)

import datetime as dt
from fastapi.responses import JSONResponse as JSONResponse


@app.exception_handler(Exception)
async def fatal(request: Request, exc: Exception):
    # Safety net for UNHANDLED exceptions per EPIC-008-F-002-S-009-REQ-B-008
    # Mode 3 (unhandled, not expected). Reaching here is always user-fatal
    # (503 to the user) — that IS the Mode 3 definition — so tag fatal_error
    # True. The architectural goal is for Mode 3 occurrences to be RARE; each
    # one observed in the log MUST be moved to a local catch with Mode 1 or
    # Mode 2 handling.
    log.exception("unhandled exception on %s", request.url.path,
                  extra={"fatal_error": True})
    return JSONResponse(
        status_code=503,
        content={"service": "FindCare", "source": "unhandled",
                 "time": dt.datetime.now(dt.timezone.utc).isoformat()},
    )

# v2.2 Part B 7.4 — startup Mongo probe. Construct the canonical utility
# then issue an explicit ping; failure raises and the container crashes,
# HF (or local docker) restarts, and the operator sees the restart loop
# and reads the logs. Steady-state degraded mode in _get_db() remains for
# transient runtime blips; only the startup probe is mandatory-loud.
_startup_db_probe = ChatHealthyMongoUtilities()
_startup_db_probe.getConnection("frontendUser", "ChatHealthyFrontEnd").admin.command("ping")
log.info("FindCare backend Mongo startup probe: ping OK")

# EPIC-010-F-101-S-005 (Data version management): bind runtime data
# collections from ChatHealthyConfig.DBVersions on startup, and mount the
# /admin/swap + /debug/active_collections endpoints.
bind_data_collections()
app.include_router(data_collections_router)


# ── EPIC-002-F-001-S-012: startup security-primitive verification ──
# FindCare's security primitives are nonce restamp (signs with findcare.key)
# and verify (reads peer certs). The probe loads both findcare.key and
# findcare.crt to confirm CERTS_DIR is bootstrapped. Exit codes per sysexits.h:
#   78 (EX_CONFIG)    — missing key or cert file
#   77 (EX_NOPERM)    — permission denied on cert/key
#   70 (EX_SOFTWARE)  — unexpected internal error
def decode_cert_pem(env_var: str, b64_value: str) -> bytes:
    """Decode one PEM env var. Raises on malformed base64. No logging here —
    the caller decides what to do with the failure."""
    import base64
    try:
        return base64.b64decode(b64_value.strip())
    except Exception as exc:
        raise ChatHealthyException(
            mode="startup_invalid_base64",
            message=f"STARTUP: {env_var} is present but not valid base64: {exc}",
            component="FindCareBackend",
            exception=exc,
        )


def try_chmod_0600(path: str) -> None:
    """Best-effort restrict file mode. Logs and continues on failure (Windows
    or non-POSIX filesystems return non-fatal errors). Never raises."""
    try:
        os.chmod(path, 0o600)
    except Exception as exc:
        # Mode 1 (REQ-B-008): best-effort startup chmod; system continues
        # without the restriction. log.info + default debug-gated.
        log.info(
            "STARTUP: chmod 0600 on %s failed (continuing): %s", path, exc,
            exc=ChatHealthyException(
                mode="startup_chmod_failed",
                message=f"STARTUP: chmod 0600 on {path} failed (continuing): {exc}",
                component="FindCareBackend",
                exception=exc,
            ),
        )


def write_certs_to_runtime_dir(mapping: dict[str, str], runtime_dir: str) -> list[str]:
    """For each present env var, decode and write to runtime_dir. Returns the
    list of filenames written. Logs the final summary on success."""
    wrote = []
    for env_var, filename in mapping.items():
        b64 = os.environ.get(env_var)
        if not b64:
            continue
        pem_bytes = decode_cert_pem(env_var, b64)
        os.makedirs(runtime_dir, exist_ok=True)
        path = os.path.join(runtime_dir, filename)
        with open(path, "wb") as f:
            f.write(pem_bytes)
        try_chmod_0600(path)
        wrote.append(filename)
    if wrote:
        log.info("startup bootstrap: wrote %s to %s (CERTS_DIR=%s)",
                 ",".join(wrote), runtime_dir, runtime_dir)
    return wrote


def bootstrap_certs_from_env():
    """Write PKI material from env vars to a runtime dir and point CERTS_DIR at it.

    HF Spaces don't support bind-mounted cert directories. The deploy pipeline
    stores the signing key and public cert as HF Space secrets (base64-encoded
    PEM). On startup we decode them to /tmp/ch_certs and set CERTS_DIR.

    If none of the env vars are present (e.g. local dev, local Docker with a
    bind-mounted /certs), the function is a no-op — the caller's CERTS_DIR
    resolution remains in effect. Malformed PEM content raises, which the
    startup check turns into an exit-78 abend per EPIC-002-F-001-S-012.
    """
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
    wrote = write_certs_to_runtime_dir(mapping, runtime_dir)
    if wrote:
        os.environ["CERTS_DIR"] = runtime_dir

def startup_security_verification():
    """EPIC-002-F-001-S-012: exercise the security primitives this
    service uses. FindCare does NOT manufacture auth tokens — that's
    SharedServices's /auth/issue. FindCare's primitives are nonce restamp
    (signs with findcare.key) and verify (reads the page-owner's cert).
    Probe loads both to confirm CERTS_DIR is bootstrapped and the
    cryptography primitives can parse them."""
    bootstrap_certs_from_env()
    try:
        from chathealthy_lib.authentication.session_token import cert_basename
        from cryptography.hazmat.primitives import serialization
        from cryptography.x509 import load_pem_x509_certificate
    except ImportError as _imp:
        # Mode 2 (REQ-B-008): startup-time fatal — handled locally by
        # logging + sys.exit(78). The process abends cleanly with a named
        # exit code so the operator can diagnose; the user never sees the
        # service since it never bound a port. Not Mode 3 because the
        # exception IS caught and handled with explicit abend semantics.
        raise ChatHealthyException(
            mode="startup_abend_config",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=78 primitive=crypto reason=import_failed: %s" % (_imp,)),
            exit_code=78,
            exception=_imp)
    certs_dir = os.environ.get("CERTS_DIR", "/certs")
    key_path = os.path.join(certs_dir, f"{cert_basename('FindCare')}.key")
    cert_path = os.path.join(certs_dir, f"{cert_basename('FindCare')}.crt")
    try:
        with open(key_path, "rb") as _f:
            serialization.load_pem_private_key(_f.read(), password=None)
        with open(cert_path, "rb") as _f:
            load_pem_x509_certificate(_f.read())
    except FileNotFoundError as _fnf:
        # Mode 2 (REQ-B-008): startup-time fatal — handled locally with
        # named exit code 78 (EX_CONFIG, missing cert/key file).
        raise ChatHealthyException(
            mode="startup_abend_config",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=78 primitive=session_token reason=missing_cert_or_key: %s" % (_fnf,)),
            exit_code=78,
            exception=_fnf)
    except PermissionError as _perm:
        # Mode 2 (REQ-B-008): startup-time fatal — handled locally with
        # named exit code 77 (EX_NOPERM, permission denied on cert/key).
        raise ChatHealthyException(
            mode="startup_abend_permission",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=77 primitive=session_token reason=permission: %s" % (_perm,)),
            exit_code=77,
            exception=_perm)
    except Exception as _exc:
        # Mode 2 (REQ-B-008): startup-time fatal — handled locally with
        # named exit code 70 (EX_SOFTWARE, key/cert unreadable for other
        # reasons). Process abends cleanly; user never sees the service.
        raise ChatHealthyException(
            mode="startup_abend_software",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=70 primitive=session_token reason=key_or_cert_unreadable: %s" % (_exc,)),
            exit_code=70,
            exception=_exc)
    log.info("startup security check PASSED — findcare.key + findcare.crt OK at %s", certs_dir)

startup_security_verification()

@app.middleware("http")
async def log_requests(request: Request, call_next):
    start = time_mod.time()
    response = await call_next(request)
    elapsed = round((time_mod.time() - start) * 1000)
    log.info("REQUEST %s %s → %d (%dms) from %s",
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
    # EPIC-002-F-003-S-004: when the chat detects a register/sign-in
    # intent it sets this field instead of running the normal pipeline.
    # The chat iframe forwards it to the wrapper as
    # postMessage(type: "gui:initiate-oauth-google").
    oauth_init: Optional[str] = None

class SearchRequest(BaseModel):
    """Direct provider search — bypasses Claude. Used for pagination."""
    specialty_query: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    zip: Optional[str] = None
    npi: Optional[str] = None
    nucc_codes: Optional[list[str]] = None
    after_npi: Optional[str] = None
    limit: int = 25
    # Named outright rather than searched for by what they do.
    last_name: Optional[str] = None
    first_name: Optional[str] = None
    middle_name: Optional[str] = None
    # Preferences the person stated. Applied to an already-narrow result.
    provider_sex: Optional[str] = None
    sole_proprietor: Optional[bool] = None
    insurance: Optional[str] = None

@app.post("/search")
async def search(body: SearchRequest):
    """Direct provider search — for pagination. No LLM involved.

    Local catch with mode discrimination per EPIC-008-F-002-S-009-REQ-B-008.
    Catcher classifies caught ChatHealthyException by mode and acts per the
    three-mode taxonomy."""
    params = body.model_dump(exclude_none=True)
    try:
        return find_care.search_providers(**params)
    except ChatHealthyException as exc:
        if exc.mode == "mongo_query_timeout":
            # Mode 2 (REQ-B-008): resource temporarily unavailable (Mongo
            # aggregate exceeded its timeout budget). Surface a graceful
            # user-facing 200 response carrying an error string; NOT 503;
            # no fatal_error tag.
            log.error("search Mode 2: mongo_query_timeout on %s.%s",
                      exc.context.get("db"), exc.context.get("coll"),
                      exc=exc, if_not_debug_log=True)
            return {
                "providers": [],
                "total_count": 0,
                "error": "Provider search is taking longer than usual. "
                         "Please try the same search again in a moment.",
                "error_mode": exc.mode,
            }
        # Unknown ChatHealthyException mode at this site → re-raise so the
        # Mode 3 safety net handles it. Adding a known mode here with its
        # Mode 1 / Mode 2 / Mode 3 classification is the way to bring it
        # under local control.
        raise


class ClassifyRequest(BaseModel):
    """GOV-011: AI translates the user's question into structured search parameters.
    One AI call. System answers with DB query after."""
    message: str

def _require_db_for_classify():
    """Guard extracted so /classify does not both raise and log in one body.

    Rule-005 statement 3: the thrower does not log, the catcher does. The
    exception still propagates into classify's existing except block, so
    behaviour is unchanged.
    """
    db = get_db()
    if db is None:
        raise ChatHealthyException(
            mode="mongo_network_failure",
            component="FindCareBackend",
            message="Mongo unavailable",
        )
    return db


@app.post("/classify")
async def classify(body: ClassifyRequest, request: Request):
    """EPIC-006-F-002-S-001: specialty matching.

    normalize -> embed -> $vectorSearch -> LLM filter. Semantic search
    carries recall; the LLM call carries precision. CAND_FLOOR is the
    handoff between them and was tuned by the operator over a week.

    This pipeline was replaced on 2026-05-10 (bc102984) by a single call
    that walked the whole NUCC corpus. Nothing asked for that, the commit
    that did it describes a sub-iframe and a label flip, and the tuning
    went with it. SpecialtyFilter was never removed -- it stayed
    instantiated and unreachable -- so this is a restoration, not a
    rewrite, and the stages below are untouched.
    """
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")

    import uuid as _uuid
    from datetime import datetime as dt, timezone as _tz

    # find_specialties is synchronous and makes blocking model calls;
    # /classify is async and already inside an event loop.
    result = await asyncio.to_thread(specialty_service.find_specialties, body.message)

    if "error" in result:
        # REQ-B-002/B-003: sanitized outward, full detail kept server-side
        # under a request id. filter.py reports "<stage>: <type>: <detail>",
        # so the stage survives and the leak does not.
        req_id = _uuid.uuid4().hex[:8]
        ts = dt.now(_tz.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        raw = result["error"]
        stage = (raw.split(":", 1)[0].strip() if ":" in raw else "unknown")
        log.error("classify req_id=%s ip=%s stage=%s detail=%r message=%r",
                  req_id, ip, stage, raw, body.message,
                  exc=ChatHealthyException(
                      mode="classify_failed",
                      message=f"classify req_id={req_id} ip={ip} stage={stage} detail={raw}",
                      component="FindCareBackend",
                  ), if_not_debug_log=True)
        return {"specialties": [], "error": sanitized_classify_error(stage, ts, req_id)}

    specialties = [
        {"code": s["Code"], "name": s["Display Name"],
         "can_prescribe": s.get("can_prescribe", False),
         "homeopathic": s.get("homeopathic", False),
         "rank": s.get("rank", 0)}
        for s in result.get("specialties", [])
    ]
    return {
        "specialties": specialties,
        "homeopathic_generalists": [],
        "complaint": result.get("complaint", ""),
        "model": "normalize + embed + vectorSearch + LLM filter",
    }


def sanitized_classify_error(stage: str, ts: str, req_id: str) -> str:
    return (f"FindCare /classify temporarily unavailable "
            f"(stage: {stage}) at {ts}. Ref: {req_id}")


@app.post("/welcome")
def welcome():
    return {"message": WELCOME_MESSAGE}


# Clinical trials cross-service entry point. SharedServices dispatches
# /findClinicalTrials utterances through this endpoint instead of
# importing the tool directly, so the clinical-trials domain stays
# inside FindCare. Streams the tool's chunk events as NDJSON; SS
# forwards each line into the user's /gate stream.
class _ClinicalTrialsRequest(BaseModel):
    condition: str
    user_location: Optional[str] = None
    age_years: Optional[int] = None
    sex: Optional[str] = None
    geographic_scope: Optional[str] = None
    page_size: int = 10
    cursor: Optional[str] = None


@app.post("/clinical_trials")
async def clinical_trials(body: _ClinicalTrialsRequest):
    import asyncio
    import json as _json
    from fastapi.responses import StreamingResponse
    try:
        from ClinicalTrials import clinical_trials_tool
    except ImportError:
        from FindCare.ClinicalTrials import clinical_trials_tool

    queue: asyncio.Queue = asyncio.Queue()
    sentinel = object()

    class _StreamCollector:
        def stream(self, event):
            queue.put_nowait(event)

    deps = _StreamCollector()
    req = clinical_trials_tool.Request(**body.model_dump())

    async def runner():
        try:
            await clinical_trials_tool.TOOL.run(deps, req)
        finally:
            queue.put_nowait(sentinel)

    asyncio.create_task(runner())

    async def gen():
        while True:
            item = await queue.get()
            if item is sentinel:
                break
            yield _json.dumps(item).encode() + b"\n"

    return StreamingResponse(gen(), media_type="application/x-ndjson")


# Provider Detail click-path endpoint (EPIC-006-F-025). Pure deterministic
# tool; no LLM. Input fields mirror the on-screen provider card.
from ProviderDetail.provider_detail_models import (
    ProviderDetailInput, ProviderDetailOutput,
)

@app.post("/provider-detail")
def provider_detail(
    body: ProviderDetailInput,
    background_tasks: BackgroundTasks,
) -> ProviderDetailOutput:
    return provider_detail_service.lookup(
        provider_name=body.name or "",
        npi=body.npi,
        state=body.state or "",
        provider_coll=providers_coll(),
        specialty_meta_coll=specialty_meta_coll,
        schedule_background_task=background_tasks.add_task,
    )



REQUIRED_INDEXES = [
    ("providers", providers_coll, ["provider_vector_index"]),
    ("SpecialtyMetaData", specialty_meta_coll, ["specialty_vector_index"]),
]

def check_indexes() -> dict:
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
    missing = []
    errors = []
    for coll_label, coll_fn, index_names in REQUIRED_INDEXES:
        try:
            existing = [idx.get("name") for idx in coll_fn().list_search_indexes()]
        except Exception as exc:
            # Mode 2 (REQ-B-008): the index check failed for this
            # collection; the error is surfaced into the errors[] list
            # and /health reports status="fail". Operator MUST know —
            # missing vector indexes mean search is broken.
            log.error("index check on %s failed: %s", coll_label, exc, exc=ChatHealthyException(
                                                                        mode="index_check_failed",
                                                                        message=f"index check on {coll_label} failed: {exc}",
                                                                        component="FindCareBackend",
                                                                        exception=exc,
                                                                    ), if_not_debug_log=True)
            errors.append({"collection": coll_label, "error": f"{type(exc).__name__}: {exc}"})
            continue
        for idx in index_names:
            if idx not in existing:
                missing.append(f"{coll_label}/{idx}")
    status = "ok" if not missing and not errors else "fail"
    return {"status": status, "missing": missing, "errors": errors}

# graph-exempt: health check — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
BUILD_INFO_PATH = "/app/build_info.json"


def read_build_info():
    """Baked-at-build-time build/version/framework. Returns None if the
    file is absent (older image); caller falls back to frontEndAdmin.BuildVersions."""
    from pathlib import Path
    p = Path(BUILD_INFO_PATH)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as _exc:
        # Mode 1 (REQ-B-008): caller falls back to placeholder build info;
        # operation continues. log.info + default debug-gated.
        log.info("build_info read failed (ignored, caller falls back): %s", _exc, exc=ChatHealthyException(
                                                                                      mode="build_info_read_failed",
                                                                                      message=f"build_info read failed (ignored, caller falls back): {_exc}",
                                                                                      component="FindCareBackend",
                                                                                      exception=_exc,
                                                                                  ))
        return None


@app.post("/health")
def health():
    """Health-state report. Returns 200 always — the body's `status` field
    carries the result. /health is a state report, not a fatal-trigger.

    Source priority for build/version/framework:
      1. /app/build_info.json — baked at image build time, truthful about
         what's actually running.
      2. frontEndAdmin.BuildVersions latest doc — legacy fallback for older images.
    """
    env_label = ENV_PREFIX if os.getenv("SPACE_ID") else "local"
    idx_check = check_indexes()
    _build = None
    _version_str = None
    _git_number = None
    _commit = None
    _built_at = None
    _version_error = None
    _source = None
    db = get_db()
    mongo_doc = {}
    if db is not None:
        try:
            mongo_doc = db["frontEndAdmin"]["BuildVersions"].find_one(sort=[("from", -1)]) or {}
        except Exception as _exc:
            # Mode 2 (REQ-B-008): Mongo read for /health version info failed;
            # endpoint still returns a body but version fields are empty.
            # Operator MUST know about Mongo unreachability.
            log.error("/health: MongoDB read for build/version/framework failed: %s", _exc, exc=ChatHealthyException(
                                                                                               mode="health_mongo_read_failed",
                                                                                               message=f"/health: MongoDB read for build/version/framework failed: {_exc}",
                                                                                               component="FindCareBackend",
                                                                                               exception=_exc,
                                                                                           ), if_not_debug_log=True)
            _version_error = f"{type(_exc).__name__}: {_exc}"

    baked = read_build_info()
    if baked is not None:
        _build = baked.get("build")
        _commit = baked.get("commit")
        _built_at = baked.get("built_at")
        _version_str = baked.get("version") or mongo_doc.get("version")
        _git_number = baked.get("commit") or mongo_doc.get("git_number")
        _source = "build_info.json"
    else:
        _build = mongo_doc.get("build")
        _version_str = mongo_doc.get("version")
        _git_number = mongo_doc.get("git_number")
        _source = "frontEndAdmin.BuildVersions"

    db_status = "connected" if db is not None and _version_error is None else (
        "unavailable" if db is None else "unreachable")
    status = "ok" if (idx_check["status"] == "ok" and db_status == "connected") else "degraded"
    result = {"status": status,
              "service": "find_care",
              "db": db_status,
              "env": env_label,
              "build": _build,
              "commit": _commit,
              "built_at": _built_at,
              "version": _version_str,
              "git_number": _git_number,
              "source": _source}
    if idx_check.get("missing"):
        result["missing_indexes"] = idx_check["missing"]
        log.error("HEALTH CHECK: missing indexes — %s", idx_check["missing"])
    if _version_error:
        result["version_error"] = _version_error
    # v2.2 Part B 7.6 — return 503 instead of 200 when Mongo is
    # unreachable. The Website fetch wrapper paints chFatalError on 503,
    # turning /health into the visible operator surface that the
    # rotation-as-operational-response model depends on.
    if db_status != "connected":
        log.error("/health returning 503 — db not connected; result=%s",
                  result, extra={"fatal_error": True})
        return JSONResponse(status_code=503, content=result)
    return result

from chathealthy_lib.authentication import (
    AuthToken, SessionRestampRequest, SessionToken, VerifyTokenResponse,
)

ORIGIN = "FindCare"


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")
    try:
        return await chat_inner(body, request)
    except Exception as e:
        tb = traceback.format_exc()
        # Mode 2 (REQ-B-008): /chat failed; endpoint returns ChatResponse with
        # an error/error_type for the iframe to render gracefully. NOT 503;
        # no fatal_error tag.
        log.error("CHAT ERROR: %s\n%s", e, tb, exc=ChatHealthyException(
                                                mode="chat_endpoint_failed",
                                                message=f"CHAT ERROR: {e}",
                                                component="FindCareBackend",
                                                exception=e,
                                            ), if_not_debug_log=True)
        err_str = str(e)
        if "429" in err_str or "rate_limit" in err_str.lower():
            err_type, err_msg = "rate_limit", f"Rate limit hit — {err_str}"
        elif "unavailable" in err_str.lower() or "connection" in err_str.lower():
            err_type, err_msg = "db_unavailable", err_str
        else:
            err_type, err_msg = "internal", tb if DEBUG else err_str
        debug_logger.log_chat(ip, body.message, len(body.history), 0, None, None, None, err_msg, body.history)
        return ChatResponse(error=err_msg, error_type=err_type)

# ---------------------------------------------------------------------------
# GOV-011-STD-002: Extract user's search term from their message using small model.
# "find me a bone doc in VA" → "bone doc"
# "show me shrinks near Richmond" → "shrinks"
# ---------------------------------------------------------------------------
def extract_user_search_term(user_message: str) -> str:
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
        return term if term else user_message
    except Exception as exc:
        raise ChatHealthyException(
            mode="gov_011_std_002_failed",
            message=f"GOV-011-STD-002 query expansion failed: {exc}",
            component="FindCareBackend",
            exception=exc,
        )


# ---------------------------------------------------------------------------
# GOV-011-STD-001: Strip redundant summary/pagination language from LLM text
# when the system has already built a summary_message.
# ---------------------------------------------------------------------------
def strip_redundant_summary(text: str, total_count: int, page_count: int) -> str:
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
        return cleaned if cleaned else text
    except Exception as exc:
        raise ChatHealthyException(
            mode="gov_011_std_001_failed",
            message=f"GOV-011-STD-001 strip failed: {exc}",
            component="FindCareBackend",
            exception=exc,
        )


# ---------------------------------------------------------------------------
# Chat handler — sequential implementation
# ---------------------------------------------------------------------------
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "Shared"))
from llm_client import chat as llm_chat

async def chat_inner(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")

    if safety_service.try_admin_unlock(body.message, ip):
        return ChatResponse(response="Session unlocked.")
    if safety_service.is_ip_locked(ip):
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)
    if safety_service.is_emergency(body.message):
        full_history = list(body.history) + [{"role": "user", "content": body.message}]
        safety_service.lock_ip(ip, trigger_message=body.message, history=full_history)
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    # EPIC-002-F-003-S-004: register/sign-in intent → short-circuit the
    # chat pipeline and hand control back to the wrapper, which owns
    # the navigation to Google. Cheap keyword match for MVP.
    _msg_lower = body.message.lower()
    _REGISTER_PHRASES = (
        "i want to register", "want to register",
        "i want to sign in", "want to sign in",
        "i want to sign up", "want to sign up",
        "i want to log in", "want to log in",
        "i want to login", "want to login",
        "register me", "sign me in", "sign me up",
        "log me in",
    )
    if any(p in _msg_lower for p in _REGISTER_PHRASES):
        return ChatResponse(
            response="Opening Google sign-in…",
            oauth_init="google",
        )

    _CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4.1")

    user_msg_count = sum(1 for m in body.history if m.get("role") == "user")
    system = system_prompt(follow_up_check=user_msg_count > 0 and user_msg_count % 5 == 0)
    messages = list(body.history) + [{"role": "user", "content": body.message}]
    total_in = total_out = 0

    log.info("CHAT model=%s call=initial msgs=%d", _CHAT_MODEL, len(messages))
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
        tool_results = tool_router.handle_normalized_tool_calls(tool_calls, messages, format_chat_history)

        for i, tc in enumerate(tool_calls):
            tc_name = tc["function"]["name"]
            if tc_name == "find_providers":
                try:
                    last_provider_result = json.loads(tool_results[i]["content"])
                except (json.JSONDecodeError, KeyError, IndexError) as _exc:
                    # Mode 1 (REQ-B-008): the per-iteration cache of the
                    # provider tool result stays None; the chat loop still
                    # continues. No user-facing failure.
                    log.info("tool_result parse for find_providers failed (ignored): %s", _exc, exc=ChatHealthyException(
                                                                                                    mode="tool_result_parse_failed",
                                                                                                    message=f"tool_result parse for find_providers failed (ignored): {_exc}",
                                                                                                    component="FindCareBackend",
                                                                                                    exception=_exc,
                                                                                                ))
            elif tc_name == "search_clinical_trials":
                try:
                    last_trials_result = json.loads(tool_results[i]["content"])
                except (json.JSONDecodeError, KeyError, IndexError) as _exc:
                    # Mode 1 (REQ-B-008): the per-iteration cache of the
                    # clinical-trials result stays None; the chat loop
                    # still continues. No user-facing failure.
                    log.info("tool_result parse for search_clinical_trials failed (ignored): %s", _exc, exc=ChatHealthyException(
                                                                                                            mode="tool_result_parse_failed",
                                                                                                            message=f"tool_result parse for search_clinical_trials failed (ignored): {_exc}",
                                                                                                            component="FindCareBackend",
                                                                                                            exception=_exc,
                                                                                                        ))

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
        log.info("CHAT tool_loop iter=%d tools=%s", loop_iter, [tc["function"]["name"] for tc in tool_calls])
        response = llm_chat(_CHAT_MODEL, messages, tools=anthropic_tools, system=system, max_tokens=4096)
        total_in  += response.get("usage", {}).get("input_tokens", 0)
        total_out += response.get("usage", {}).get("output_tokens", 0)

    text = response.get("content", "")
    # Replace bare "Skip Snow on LinkedIn" with its markdown link form,
    # but only when it isn't already in markdown-link form
    # (i.e., not surrounded by `[` and `]`). No regex per Rule-008.
    _LI_TARGET = "Skip Snow on LinkedIn"
    _LI_REPLACEMENT = "[Skip Snow on LinkedIn](https://linkedin.com/in/skipsnow)"
    _li_out = []
    _li_i = 0
    while True:
        _li_idx = text.find(_LI_TARGET, _li_i)
        if _li_idx == -1:
            _li_out.append(text[_li_i:])
            break
        _li_prev = text[_li_idx - 1] if _li_idx > 0 else ""
        _li_after_idx = _li_idx + len(_LI_TARGET)
        _li_next = text[_li_after_idx] if _li_after_idx < len(text) else ""
        _li_out.append(text[_li_i:_li_idx])
        if _li_prev == "[" and _li_next == "]":
            _li_out.append(_LI_TARGET)
        else:
            _li_out.append(_LI_REPLACEMENT)
        _li_i = _li_after_idx
    text = "".join(_li_out)

    pagination = None
    if last_provider_result and isinstance(last_provider_result, dict):
        total_count = last_provider_result.get("total_count", 0)
        if total_count > 0:
            from ProviderManagement.provider_search_service import FindCareService
            user_term = extract_user_search_term(body.message)
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
            user_msg = extract_user_search_term(body.message)
            parts = [f"Found {trial_count} recruiting trial{'s' if trial_count != 1 else ''} for '{user_msg}'."]
            if has_travel:
                parts.append(" Travel times included.")
            parts.append(f" [View all on ClinicalTrials.gov](https://clinicaltrials.gov/search?cond={requests_lib.utils.quote(user_msg)}&aggFilters=status:rec)")
            trials_meta = TrialsMeta(
                trial_count=trial_count,
                condition=user_msg,
                summary_message="".join(parts),
            )

    if pagination and pagination.summary_message and text:
        text = strip_redundant_summary(text, pagination.total_count, pagination.count)

    log.info("CHAT complete tokens_in=%d tokens_out=%d pagination=%s trials=%s",
              total_in, total_out, bool(pagination), bool(trials_meta))
    debug_logger.log_chat(ip, body.message, len(body.history), loop_iter, total_in, total_out, text, None)
    return ChatResponse(response=text, tokens_in=total_in, tokens_out=total_out,
                        pagination=pagination, trials=trials_meta)

# ---------------------------------------------------------------------------
# Static files
# ---------------------------------------------------------------------------
static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(static_dir):
    from starlette.staticfiles import StaticFiles
    from starlette.responses import FileResponse

    # graph-exempt: static page render — no business logic; per BUG-ARCH-GRAPH-EXEMPT-001
    @app.get("/")
    async def serve_index():
        return FileResponse(
            os.path.join(static_dir, "index.html"),
            headers={"Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0"})

    app.mount("/assets", StaticFiles(directory=os.path.join(static_dir, "assets")), name="assets")

if __name__ == "__main__":
    import uvicorn
    kwargs = {"host": "0.0.0.0", "port": int(os.getenv("PORT", "7860"))}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
