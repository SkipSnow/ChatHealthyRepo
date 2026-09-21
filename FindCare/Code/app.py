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
from chathealthy_lib import gate as ch_gate
from pydantic import BaseModel
import requests as requests_lib

# FindCare/ on sys.path so business-model tools (SpecialtyFilter,
# ProviderManagement) are importable. Must happen BEFORE the imports
# below that pull from those packages. Dockerfile COPYs FindCare/ into
# /app/FindCare so the same relative walk resolves in the container.

log = ChatHealthyLoggingService()


def _application_root() -> str:
    """The root everything below is stated from.

    These used to be four "..' hops from this file, true only while it sat
    at Code/ConversationalUX/FindCareChat/backend. It now sits at
    FindCare/Code, two levels shallower, and the container died on
    "No module named 'externalInterface'" because the walk climbed past the
    root. A hop count is a claim about where a file lives.

    The marker is brain/, present in the repository and COPYd into the
    image, so the same lookup resolves in both.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidate = here
    while True:
        if os.path.isdir(os.path.join(candidate, "brain")):
            return candidate
        parent = os.path.dirname(candidate)
        if parent == candidate:
            return here
        candidate = parent


_ROOT = _application_root()

sys.path.insert(0, os.path.join(_ROOT, "FindCare"))
# sharedServices/Code/ on sys.path for the same reason: a widget or a
# service lives with the feature that owns it, and several of those
# features are shared ones.
sys.path.insert(0, os.path.join(_ROOT, "sharedServices", "Code"))

# ARCH-001 — domain services
from externalInterface.tool_router import ToolRouter
from find_care_facade import FindCareFacade
from ProviderManagement.provider_search_service import FindCareService
from SpecialtyFilter.filter import (
    SpecialtyFilter, SECTION_INDIVIDUAL, SECTION_ORGANIZATION,
    facility_groups,
)
from ClinicalTrials.clinical_trials_service import ClinicalTrialsService
from ProviderDetail.provider_detail_service import ProviderDetailService
from safety_service import SafetyService
from AboutChatHealthy.about_service import AboutService
from ProviderManagement.provider_search_models import ProviderSearchInput, SpecialtyInput
from ProviderManagement.facility_utterance import mine_facility_parameters
from ProviderManagement.individual_provider_utterance import (
    mine_individual_provider_parameters,
)
from ProviderManagement.nucc_utterance import mine_nucc_parameters
from ProviderManagement.clinical_trial_utterance import (
    mine_clinical_trial_parameters,
)
from ClinicalTrials.clinical_trials_models import ClinicalTrialsInput
from provider_lookup_models import ProviderLookupInput
from embedding_client import EmbeddingClient
from logs.debug_logger import DebugLogger

load_dotenv(override=True)

# Shared utilities. This reached Code/Shared, which the refactor removed;
# prompt_system_maker moved here, beside this file, and this directory is
# already on the path as the script's own.
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
from chathealthy_lib import http_request_facts as request_facts
from chathealthy_lib.exceptions import ChatHealthyException
from prompt_system_maker import PromptSystemMaker

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
from db_config import (  # drained from app.py
    ENV_PREFIX, get_db, SESSION_DB, SESSION_COLLECTION,
    INDIVIDUAL_PROVIDER_PAGE, NUCC_PAGE, FACILITY_PAGE, CLINICAL_TRIAL_PAGE)
from page_parameters import (  # drained from app.py
    parameter_entry as _parameter_entry,
    parameters_in_force as _parameters_in_force,
    write_page_parameters as _write_page_parameters)
from route_support import (  # drained from app.py
    unmet_requirements as _unmet_requirements,
    geography_known as _geography_known,
    read_page_parameter as _read_page_parameter,
    gesture_entry as _gesture_entry,
    open_record_attribute as _open_record_attribute,
    clear_page_parameter as _clear_page_parameter,
    question_for as _question_for,
    searched_codes as _searched_codes,
    sex_code as _sex_code,
    provider_page_entries as _provider_page_entries)
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
# get_db and ENV_PREFIX are drained to db_config.py (imported above).

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
brain_dir = os.path.join(_ROOT, "brain")
prompt_maker = PromptSystemMaker(brain_dir=brain_dir, env_prefix=ENV_PREFIX)
EMERGENCY_KEYWORDS = prompt_maker.load_emergency_keywords()
anthropic_tools = prompt_maker.load_tool_definitions()
WELCOME_MESSAGE = PromptSystemMaker.build_welcome_message()
# Build/version/framework: live from MongoDB per EPIC-008-F-004-S-001

ME_DIR = os.getenv("ME_DIR") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "me")
if not os.path.isdir(ME_DIR):
    ME_DIR = os.path.join(_ROOT, "sharedServices", "Code",
                          "AboutChatHealthy", "me")
ME = prompt_maker.load_me_context(ME_DIR)

# UAT report
# UAT report: local repo path or HF flat layout
def system_prompt(follow_up_check: bool = False) -> str:
    return prompt_maker.build_system_prompt(emergency_response=EMERGENCY_RESPONSE, follow_up_check=follow_up_check)

# ---------------------------------------------------------------------------
# Service initialization — ARCH-001
# ---------------------------------------------------------------------------
# embedding_client, specialty_service and find_care are drained to services.py.
from services import embedding_client, specialty_service, find_care  # noqa: E402

clinical_trials_service = ClinicalTrialsService()
provider_detail_service = ProviderDetailService()
find_care_facade = FindCareFacade(
    clinical_trials=clinical_trials_service, provider_detail=provider_detail_service)

safety_service = SafetyService(get_db_fn=get_db, env_prefix=ENV_PREFIX, emergency_keywords=EMERGENCY_KEYWORDS)
about_service = AboutService(me_context=ME, trim_fn=PromptSystemMaker.trim)

debug_logger = DebugLogger(get_db_fn=get_db, env_prefix=ENV_PREFIX)

# ToolRouter — F-05 fix
tool_router = ToolRouter()
tool_router.register_with_models([
    ("find_providers",          find_care.search_providers,            ProviderSearchInput),
    ("find_specialty_codes",    find_care.identify_specialty,          SpecialtyInput),
    ("search_clinical_trials",  find_care_facade.search_clinical_trials,  ClinicalTrialsInput),
    ("lookup_provider_external", find_care_facade.get_provider_details,   ProviderLookupInput),
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

# The transport shell, once. CORS and the two exception handlers come from
# the generic Gate in chathealthy_lib.gate; FindCare names its own component
# and origins. FindCare is a satellite — it carries no navigator, so it
# mounts no /gate; its business routes are registered below.
app = ch_gate.build_gate_app(
    title="ChatHealthy FindCare API",
    component="FindCare",
    cors_allow_origins=["https://chathealthy.ai", "https://www.chathealthy.ai", "https://dev.chathealthy.ai"],
    cors_allow_origin_regex=r"https://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
    cors_allow_credentials=False,
)

from chathealthy_lib.runtime_data_collections import (  # noqa: E402
    declared_attributes, optional_parameters, required_parameters)
from ProviderManagement.utterance_mining import ask_for_missing  # noqa: E402
from chathealthy_lib.runtime_data_collections import (
    providers_coll,
    specialty_meta_coll,
    bind_from_manifest as bind_data_collections,
    router as data_collections_router,
)

import datetime as dt
from fastapi.responses import JSONResponse as JSONResponse


# The ChatHealthyException-to-response and unhandled-fault (503) handlers
# are installed by chathealthy_lib.gate.build_gate_app above.

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
def startup_security_verification():
    """Exercise the security primitive this service uses, at startup.

    FindCare verifies session tokens SharedServices signed for the FindCare
    pair. That certificate is named by ChatHealthyConfig.CertificateRegistry
    and held in the vault, so the probe is a resolution and a parse. No
    certificate is written to this container's filesystem, and none is read
    from it.

    Exit codes per sysexits.h: 78 (EX_CONFIG) when the credential cannot be
    resolved, 70 (EX_SOFTWARE) when it resolves and will not parse.
    """
    try:
        from chathealthy_lib.authentication.session_token import (
            DEFAULT_READER, TOKEN_SIGNER)
        from chathealthy_lib.authentication.signing_credential import verifying_cert
        from cryptography.x509 import load_pem_x509_certificate
    except ImportError as _imp:
        raise ChatHealthyException(
            mode="startup_abend_config",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=78 primitive=crypto reason=import_failed: %s" % (_imp,)),
            exit_code=78,
            exception=_imp)
    try:
        pem = verifying_cert(TOKEN_SIGNER, DEFAULT_READER)
    except ChatHealthyException as _res:
        raise ChatHealthyException(
            mode="startup_abend_config",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=78 primitive=session_token "
                     "reason=credential_unresolvable: %s" % (_res,)),
            exit_code=78,
            exception=_res)
    try:
        load_pem_x509_certificate(pem.encode())
    except Exception as _exc:
        raise ChatHealthyException(
            mode="startup_abend_software",
            component="FindCareBackend",
            message=("STARTUP ABEND exit=70 primitive=session_token "
                     "reason=cert_unparseable: %s" % (_exc,)),
            exit_code=70,
            exception=_exc)
    log.info("startup security check PASSED - registered certificate for "
             "%s -> FindCare resolved from the vault", TOKEN_SIGNER)


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

# CORS is installed by chathealthy_lib.gate.build_gate_app above.

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

SHARED_SERVICES_ORIGIN = "SharedServices"


def require_gateway_signature(session_token: Optional[dict],
                              posted: Optional[dict] = None,
                              headers: Optional[dict] = None) -> None:
    """Refuse a request that did not arrive through the approved gateway,
    and state what it carries for the code that serves it.

    EPIC-006-F-001-S-003-REQ-B-004 has two halves. Arriving through the
    gateway is met by the client having no other address to call; refusing
    a request that arrived by another route can only be met here, inside
    FindCare, because the Space is a public HTTPS host and cannot require
    a client certificate.

    SharedServices signs the session token with the private half of the
    SharedServices-to-FindCare pair, and FindCare verifies it against the
    public half, both named by ChatHealthyConfig.CertificateRegistry and
    held in the vault. A token signed for another peer does not verify
    here. FindCare does not re-validate the session -- /gate has already
    done that, and a second validation with a different answer would be
    worse than none.
    """
    if not session_token:
        raise ChatHealthyException(
            mode="http_error",
            component="FindCareBackend",
            message="request carries no session token; every FindCare route "
                    "requires one bearing a SharedServices signature",
            status_code=401,
        )
    token = SessionToken.model_validate(session_token)
    if not token.verify(expected_origin=SHARED_SERVICES_ORIGIN):
        raise ChatHealthyException(
            mode="http_error",
            component="FindCareBackend",
            message="session token does not bear a valid SharedServices "
                    "signature",
            status_code=401,
        )
    # This is the door, and every route passes through it, so this is where
    # the request's facts are stated. Code below reads them from
    # http_request_facts instead of taking them as arguments.
    request_facts.state_the_facts(
        token=token, posted=posted or {}, headers=headers or {})


class SearchRequest(BaseModel):
    """Direct provider search. Used for pagination.

    entity_type carries no default: it is a property of the page the request
    was dispatched to, so a request that does not name it is refused rather
    than falling through to one page's answer.
    """
    entity_type: str
    specialty_query: Optional[str] = None
    state: Optional[str] = None
    city: Optional[str] = None
    county: Optional[str] = None
    zip: Optional[str] = None
    npi: Optional[str] = None
    # A set of records addressed by identity. The general shape; a single
    # npi is a case of it. A caller holding several identities asks once
    # rather than opening the collection itself (C-26).
    npis: Optional[list[str]] = None
    nucc_codes: Optional[list[str]] = None
    # The keyset position and which way the page is taken from it.
    cursor: Optional[str] = None
    direction: str = "forward"
    limit: int = 25
    # The gateway's signature, verified before anything else happens.
    session_token: Optional[dict] = None
    # How a facility is named: outright, or by the person who administers
    # it. Undeclared here they were dropped at this boundary, so a search
    # that named an administrator returned every organization instead.
    facility_name: Optional[str] = None
    administrator_last_name: Optional[str] = None
    administrator_first_name: Optional[str] = None
    administrator_middle_name: Optional[str] = None
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
    """Collect the request, prove the gateway signature, hand the turn to the
    page-operations pager."""
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    params = body.model_dump(exclude_none=True)
    params.pop("session_token", None)
    from page_operations import search as _search_op
    return await _search_op(params)


class FacilityFindRequest(BaseModel):
    """The facility page's own request: an utterance and the talk before it.

    The page mines what it needs from these two things; nothing upstream
    assembles its parameters for it.
    """
    session_token: dict
    utterance: str
    history: list = []


# FACILITY_PAGE, SESSION_DB, SESSION_COLLECTION drained to db_config.py.


# _facility_kinds, _facility_page_entries, _write_facility_parameters drained
# to ProviderManagement/facility_page.py.


@app.post("/facility/find")
async def facility_find(body: FacilityFindRequest):
    """Collect the request, prove the gateway signature, hand the turn to the
    facility page handler."""
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from ProviderManagement.facility_page import find
    return await find(body.session_token, body.utterance, body.history)


# Which tool each of this service's surfaces is, as the configuration
# names them. A page is a list of tools and holds their values; what a
# tool requires is the tool's, because a page carries several and a
# search runs before any record is opened.
PROVIDER_SEARCH_TOOL = "ProviderSearch"
FACILITY_SEARCH_TOOL = "FacilitySearch"
SPECIALTY_FILTER_TOOL = "SpecialtyFilter"
CLINICAL_TRIALS_TOOL = "ClinicalTrials"

# INDIVIDUAL_PROVIDER_PAGE, NUCC_PAGE, CLINICAL_TRIAL_PAGE drained to db_config.py.

# Shared route helpers (unmet_requirements, geography_known, state_of_zip,
# read/clear page parameter, gesture_entry, open_record_attribute) drained to
# route_support.py and imported above.


class FacilityPageRequest(BaseModel):
    """Another page of the facility list, forward or back.

    The caller sends where in the list to continue from and nothing else.
    What the list is a list OF -- the place, the kind, the name, the
    administrator -- is this page's, already in force from the search that
    produced the list being paged.
    """
    session_token: dict
    cursor: str
    direction: str = "forward"
    limit: int = 25


@app.post("/facility/page")
async def facility_page(body: FacilityPageRequest):
    """Collect the request, prove the gateway signature, hand the turn to the
    facility page handler's pager."""
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from ProviderManagement.facility_page import page
    return await page(body.cursor, body.direction, body.limit)


class WhatIsNeededRequest(BaseModel):
    """A turn that produced nothing, asking what was lacking.

    Both are named because they answer different halves. The page is the
    scope the values live in -- a geography on the care-giver page is not
    the geography on the facility page. The tool is what has requirements:
    a page carries several, and a search runs before any record is opened.
    """
    session_token: dict
    page: str
    tool: str
    utterance: str = ""
    history: list = []


@app.post("/page/what-is-needed")
async def page_what_is_needed(body: WhatIsNeededRequest):
    """What this page still needs, and the question that asks for it.

    A turn that showed nothing and said nothing leaves the person with a
    dead screen. The page is the only thing that knows why: it holds the
    declaration saying which of its attributes it cannot run without, and
    the session saying which are in force.

    Read from the session rather than from the turn, because the turn may
    have written nothing -- what matters is what the page has, however it
    came to have it.
    """
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from page_operations import what_is_needed
    return await what_is_needed(body.page, body.tool, body.utterance,
                                body.history)


class DetailOpenRequest(BaseModel):
    """A record the person navigated to, on a named page."""
    session_token: dict
    page: str
    record_id: str


class DetailCloseRequest(BaseModel):
    """A page that has stopped showing a record."""
    session_token: dict
    page: str


@app.post("/page/detail-open")
async def page_detail_open(body: DetailOpenRequest):
    """Record that this page is showing this record.

    An open detail is a place the person navigated to, and recording it is
    what lets a return put them back on it rather than at the top of the
    list. The caller names the page and the record; which attribute holds
    it is this service's to know.
    """
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from page_operations import detail_open
    return await detail_open(body.page, body.record_id)


@app.post("/page/detail-close")
async def page_detail_close(body: DetailCloseRequest):
    """Record that this page has stopped showing a record.

    Not clearing it is what resurrects a panel on the next return, so the
    close is a write and not merely the absence of one.
    """
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from page_operations import detail_close
    return await detail_close(body.page)


class ProviderExclusionsRequest(BaseModel):
    """Which of these care givers the filter in force sets aside.

    The caller sends identities and nothing else. Which specialties are in
    force, and what it means for one to admit a care giver, are this page's
    to know -- a caller that sent the codes would be a caller that had to
    hold them.
    """
    session_token: dict
    npis: list[str] = []


@app.post("/provider/exclusions")
async def provider_exclusions(body: ProviderExclusionsRequest):
    """Mark, per identity, whether the specialties in force admit them.

    The row is marked and kept, never dropped: a filter that silently
    discards a person's own choice is the failure this prevents
    (EPIC-006-F-001-S-002-REQ-B-019). Only this page holds a care giver's
    full taxonomy list, which is why the comparison is made here.
    """
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from page_operations import provider_exclusions as _exclusions_op
    return await _exclusions_op(body.npis)


# _question_for drained to route_support.py (imported above).
# _write_page_parameters drained to page_parameters.py (imported above).


# _resolve_specialties, _ticked, _specialty_groups are drained to services.py.
from services import (  # noqa: E402
    resolve_specialties as _resolve_specialties,
    ticked as _ticked,
    specialty_groups as _specialty_groups,
)


# _searched_codes and _sex_code drained to route_support.py (imported above).


class ProviderFindRequest(BaseModel):
    """The individual-provider page's own request: an utterance and the
    talk before it.

    The page mines what it needs from these two things; nothing upstream
    assembles its parameters for it.
    """
    session_token: dict
    utterance: str
    history: list = []


# _provider_page_entries drained to route_support.py (imported above).


@app.post("/provider/find")
async def provider_find(body: ProviderFindRequest):
    """Collect the request, prove the gateway signature, hand the turn to the
    individual-provider page handler."""
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from ProviderManagement.individual_provider_page import find
    return await find(body.utterance, body.history)


class SpecialtyFindRequest(BaseModel):
    """The NUCC page's own request: an utterance and the talk before it."""
    session_token: dict
    utterance: str
    history: list = []


@app.post("/specialty/find")
async def specialty_find(body: SpecialtyFindRequest):
    """The NUCC page mines its own complaint and offers the kinds of care
    giver that treat it.

    It finds nobody. This is the page the person is on while their
    geography is not yet usable, so what it produces is the panel and the
    codes that panel is ticked with, and nothing else.
    """
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    # The NUCC mining is owned by the specialty_filter tool; the route only
    # proves the caller and hands the turn to it.
    from SpecialtyFilter import specialty_filter_tool
    return await specialty_filter_tool.resolve_nucc_page(
        body.utterance, body.history)


class ClassifyRequest(BaseModel):
    """GOV-011: AI translates the user's question into structured search parameters.
    One AI call. System answers with DB query after."""
    message: str
    # Which partition of the catalogue this resolution reads: the
    # individual section for a care giver, the organization section for a
    # facility. The same funnel runs either way; this is the one thing its
    # queries differ by.
    section: str = "Individual"
    # The gateway's signature, verified before anything else happens.
    session_token: Optional[dict] = None

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


@app.post("/nucc/classify")
async def nucc_classify(body: ClassifyRequest, request: Request):
    """EPIC-006-F-003-S-001: specialty matching.

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
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (
        request.client.host if request.client else "unknown")
    from SpecialtyFilter.specialty_filter_tool import classify
    return await classify(body.message, body.section, ip)


@app.post("/welcome")
def welcome():
    return {"message": WELCOME_MESSAGE}


# Clinical trials cross-service entry point. SharedServices posts the
# utterance and the talk before it here; this page reads them, so both
# what a trial search is made of and the searching itself stay inside
# FindCare. Streams the criteria and then the tool's chunk events as
# NDJSON; SS forwards each line into the user's /gate stream.
class TrialFindRequest(BaseModel):
    """The clinical-trial page's own request: an utterance and the talk
    before it.

    The page mines what it needs from these two things; nothing upstream
    assembles its parameters for it.
    """
    session_token: dict
    utterance: str
    history: list = []


@app.post("/trial/find")
async def trial_find(body: TrialFindRequest):
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from ProviderManagement.clinical_trial_page import find
    return await find(body.utterance, body.history)


# Provider Detail click-path endpoint (EPIC-006-F-002). Pure deterministic
# tool; no LLM. Input fields mirror the on-screen provider card.
from ProviderDetail.provider_detail_models import ProviderDetailInput

@app.post("/provider-detail")
def provider_detail(
    body: ProviderDetailInput,
    background_tasks: BackgroundTasks,
):
    require_gateway_signature(body.session_token,
                              posted=body.model_dump(exclude_none=True))
    from ProviderDetail.provider_detail_page import detail
    return detail(body, background_tasks.add_task)


REQUIRED_INDEXES = [
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


# ---------------------------------------------------------------------------
# No browser-addressable surface
# ---------------------------------------------------------------------------
# The React application is served by the website, not from here. This Space
# therefore serves nothing a browser loads directly, which is what lets
# every route on it require a SharedServices signature -- a bundle route
# that required one could not be loaded by the iframe that needs it.

if __name__ == "__main__":
    import uvicorn
    kwargs = {"host": "0.0.0.0", "port": int(os.getenv("PORT", "7860"))}
    ssl_cert = os.getenv("SSL_CERTFILE")
    ssl_key = os.getenv("SSL_KEYFILE")
    if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
        kwargs["ssl_certfile"] = ssl_cert
        kwargs["ssl_keyfile"] = ssl_key
    uvicorn.run(app, **kwargs)
