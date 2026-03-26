# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

import json
import logging
import os
import requests
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Optional

from anthropic import Anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import BaseModel
from pypdf import PdfReader

load_dotenv(override=True)

# ---------------------------------------------------------------------------
# Shared utilities
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
# Constants
# ---------------------------------------------------------------------------
SUPPORTED_STATES = {"DE", "MS"}
_ENV_PREFIX       = os.getenv("ENV_PREFIX", "dev")
_DEBUG            = os.getenv("DEBUG", "false").lower() == "true"

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

_ip_emergency_locks: dict[str, float] = {}
_EMERGENCY_LOCK_SECONDS = 3600
_ADMIN_UNLOCK_KEY = os.getenv("ADMIN_UNLOCK_KEY", "")

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
# Safety
# ---------------------------------------------------------------------------
def _is_ip_locked(ip: str) -> bool:
    expiry = _ip_emergency_locks.get(ip)
    if expiry is None:
        return False
    if time.time() < expiry:
        return True
    del _ip_emergency_locks[ip]
    return False


def _lock_ip(ip: str) -> None:
    _ip_emergency_locks[ip] = time.time() + _EMERGENCY_LOCK_SECONDS


def _safety_collection():
    db = _get_db()
    if db is None:
        return None
    try:
        return db[f"{_ENV_PREFIX}_Safety"]["emergency_incidents"]
    except Exception:
        return None


def _lock_ip_db(ip: str, trigger_message: str = "", history: list = None) -> bool:
    _lock_ip(ip)
    col = _safety_collection()
    if col is None:
        _log.warning("SAFETY ALERT: DB unavailable — incident for %s NOT persisted.", ip)
        return False
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_EMERGENCY_LOCK_SECONDS)
    safe_history = [
        {"role": m["role"], "content": str(m.get("content", ""))[:300]}
        for m in (history or [])
        if m.get("role") in ("user", "assistant")
    ]
    try:
        col.insert_one({
            "ip": ip,
            "locked_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "trigger_message": trigger_message[:500],
            "chat_history_deidentified": safe_history,
        })
        return True
    except Exception as e:
        _log.error("SAFETY ALERT: DB write failed for %s: %s", ip, e)
        return False


def _check_ip_lock_db(ip: str) -> bool:
    if _is_ip_locked(ip):
        return True
    col = _safety_collection()
    if col is None:
        return False
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        record = col.find_one({"ip": ip, "expires_at": {"$gt": now_iso}})
        if record:
            _lock_ip(ip)
            return True
        return False
    except Exception as e:
        _log.warning("SAFETY DB check failed: %s — treating as unlocked", e)
        return False


def _admin_unlock(message: str, ip: str) -> bool:
    if not _ADMIN_UNLOCK_KEY:
        return False
    if message.strip().upper() == f"UNLOCK:{_ADMIN_UNLOCK_KEY.upper()}":
        _ip_emergency_locks.pop(ip, None)
        col = _safety_collection()
        if col is not None:
            try:
                col.delete_many({"ip": ip})
            except Exception:
                pass
        return True
    return False


def _session_is_locked(history: list) -> bool:
    return any(
        m.get("role") == "assistant" and EMERGENCY_RESPONSE in str(m.get("content", ""))
        for m in history
    )


def _safety_check(message: str) -> bool:
    msg_lower = message.lower()
    keyword_hit = any(kw in msg_lower for kw in EMERGENCY_KEYWORDS)
    ai_hit = False
    try:
        client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=64,
            messages=[{"role": "user", "content": (
                "You are a safety classifier. Decide ONLY between two outcomes:\n"
                "  EMERGENCY — call 911 or go to ER immediately\n"
                "  PROBE — ask a clarifying question first\n\n"
                "EMERGENCY requires ALL of: (1) specific body location or system named, "
                "(2) acute onset or severity stated, (3) life-threat implied.\n"
                "Examples that ARE emergencies: 'chest pain', 'I can't breathe', "
                "'I think I'm having a heart attack', 'I took too many pills', "
                "'I want to kill myself', 'I'm having a seizure'.\n\n"
                "PROBE — these are NOT emergencies:\n"
                "  'I'm in pain' — location unknown → PROBE\n"
                "  'I'm in a lot of pain' — location unknown → PROBE\n"
                "  'I don't feel well' → PROBE\n"
                "  'something hurts' → PROBE\n"
                "  'I feel sick' → PROBE\n"
                "  ANY pain without specific body location and stated severity → PROBE\n\n"
                "Return ONLY valid JSON: {\"emergency\": true|false, \"confidence\": 0.0-1.0}\n\n"
                f"Message: {message}"
            )}],
        )
        raw = resp.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        confidence = float(result.get("confidence", 0))
        ai_hit = bool(result.get("emergency", False)) and confidence >= 0.80
    except Exception as exc:
        _log.error("Safety check failed (%s) — defaulting to escalation", exc)
        return True
    return keyword_hit or ai_hit


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------
_SPARKMAIL_API_KEY = os.getenv("SPARKMAIL_API_KEY")
_SPARKMAIL_FROM    = os.getenv("NOTIFICATION_FROM_EMAIL")
_SPARKMAIL_TO      = os.getenv("NOTIFICATION_TO_EMAIL")


def push(message):
    _log.info("Push: %s", message)
    if not (_SPARKMAIL_API_KEY and _SPARKMAIL_FROM and _SPARKMAIL_TO):
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
# Database helpers
# ---------------------------------------------------------------------------
def commitSignificantActivity(payload=None, **kwargs):
    db = _get_db()
    if db is None:
        return {"recorded": "ok", "note": "MongoDB unavailable"}
    payload = payload or kwargs
    if isinstance(payload, str):
        payload = json.loads(payload)
    database   = f"{_ENV_PREFIX}_{payload['database']}"
    collection = payload["collection"]
    record     = dict(payload["record"])
    record["record_number"] = db[database][collection].count_documents({}) + 1
    record["datetime"]      = datetime.now().isoformat()
    db[database][collection].insert_one(record)
    return {"recorded": "ok"}


def _format_chat_history(messages):
    out = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c[:500]})
        elif isinstance(c, list):
            # Anthropic content blocks — extract text
            text = " ".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") for b in c)
            out.append({"role": m["role"], "content": text[:500]})
    return out


# ---------------------------------------------------------------------------
# Provider tools
# ---------------------------------------------------------------------------
def _expand_query_terms(query: str) -> list[str]:
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
    return json.loads(response.content[0].text.strip())


def find_specialty_codes(query: str) -> dict:
    db = _get_db()
    if db is None:
        return {"error": "Database unavailable"}

    projection = {"_id": 0, "Code": 1, "Classification": 1, "Specialization": 1, "Display Name": 1}
    individual_provider_groupings = [
        "Allopathic & Osteopathic Physicians",
        "Behavioral Health & Social Service Providers",
        "Chiropractic Providers",
        "Dental Providers",
        "Dietary & Nutritional Service Providers",
        "Emergency Medical Service Providers",
        "Eye and Vision Services Providers",
        "Nursing Service Providers",
        "Nursing Service Related Providers",
        "Other Service Providers",
        "Pharmacy Service Providers",
        "Physician Assistants & Advanced Practice Nursing Providers",
        "Podiatric Medicine & Surgery Service Providers",
        "Respiratory, Developmental, Rehabilitative and Restorative Service Providers",
        "Speech, Language and Hearing Service Providers",
        "Student, Health Care",
        "Technologists, Technicians & Other Technical Service Providers",
    ]
    individual_filter = {"Grouping": {"$in": individual_provider_groupings}}
    specialty_col = db[f"{_ENV_PREFIX}_PublicHealthData"]["SpecialtyMetaData"]

    def regex_pipeline():
        stems = _expand_query_terms(query)
        regex_clauses = [
            {field: {"$regex": stem, "$options": "i"}}
            for stem in stems
            for field in ("Specialization", "Display Name")
        ]
        codes = list(specialty_col.find(
            {"$and": [{"$or": regex_clauses}, individual_filter]}, projection
        )) if regex_clauses else []
        return codes, stems

    def vector_pipeline():
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        query_vector = client.embeddings.create(
            model="text-embedding-3-small", input=query
        ).data[0].embedding
        top = list(specialty_col.aggregate([
            {"$vectorSearch": {
                "index": "specialty_vector_index",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": 100,
                "limit": 5,
            }},
            {"$project": {"_id": 0, "Classification": 1, "score": {"$meta": "vectorSearchScore"}}},
        ]))
        classifications = list({m["Classification"] for m in top if m.get("score", 0) > 0.4})
        codes = list(specialty_col.find(
            {"$and": [{"Classification": {"$in": classifications}}, individual_filter]}, projection
        )) if classifications else []
        return codes, classifications

    with ThreadPoolExecutor(max_workers=2) as ex:
        rf = ex.submit(regex_pipeline)
        vf = ex.submit(vector_pipeline)
        regex_codes, stems            = rf.result()
        vector_codes, classifications = vf.result()

    seen, all_codes = set(), []
    for doc in vector_codes + regex_codes:
        if doc["Code"] not in seen:
            seen.add(doc["Code"])
            all_codes.append(doc)

    # Cap results — full NUCC dumps can exceed token budget in the tool loop
    all_codes = all_codes[:10]

    if "debug" in query.lower():
        return {
            "debug": True,
            "query": query,
            "stems_used_for_regex": stems,
            "classifications_from_vector_search": classifications,
            "total_codes_found": len(all_codes),
        }

    # Return only Code and Display Name — Classification/Specialization not needed by Claude
    return {
        "specialties": [{"Code": c["Code"], "Display Name": c.get("Display Name", "")} for c in all_codes],
        "matched_classifications": classifications,
    }


def find_providers(specialty_query: str, state: str, city: str = "", limit: int = 5) -> dict:
    state_upper = state.upper().strip()
    if state_upper not in SUPPORTED_STATES:
        return {
            "supported": False,
            "state": state_upper,
            "message": (
                f"FindCare is currently available in Delaware (DE) and Mississippi (MS) only. "
                f"We've noted interest in {state_upper}."
            ),
        }

    db = _get_db()
    if db is None:
        return {"error": "Database unavailable"}

    specialty_result = find_specialty_codes(specialty_query)
    if "error" in specialty_result:
        return specialty_result

    codes = [s["Code"] for s in specialty_result.get("specialties", [])]
    if not codes:
        return {"supported": True, "providers": [], "message": f"No matching specialty found for '{specialty_query}'."}

    query_filter = {
        "practice_address.state": state_upper,
        "taxonomies.code": {"$in": codes},
    }
    if city:
        query_filter["practice_address.city"] = {"$regex": city.strip(), "$options": "i"}

    projection = {
        "_id": 0, "npi": 1, "entity_type_code": 1,
        "provider_first_name": 1, "provider_last_name_legal_name": 1,
        "provider_middle_name": 1, "provider_name_prefix_text": 1,
        "provider_name_suffix_text": 1, "provider_credential_text": 1,
        "provider_organization_name_legal_business_name": 1,
        "practice_address": 1, "taxonomies": 1,
    }

    raw = list(
        db[f"{_ENV_PREFIX}_PublicHealthData"]["providers_staging"]
        .find(query_filter, projection)
        .limit(min(int(limit), 10))
    )

    if not raw:
        return {"supported": True, "providers": [], "message": f"No {specialty_query} providers found in {state_upper}."}

    providers = []
    for p in raw:
        if p.get("entity_type_code") == "1":
            parts = [
                p.get("provider_name_prefix_text"), p.get("provider_first_name"),
                p.get("provider_middle_name"), p.get("provider_last_name_legal_name"),
                p.get("provider_name_suffix_text"),
            ]
            name = " ".join(x for x in parts if x)
            if p.get("provider_credential_text"):
                name += f", {p['provider_credential_text']}"
        else:
            name = p.get("provider_organization_name_legal_business_name") or "Unknown Organization"

        addr = p.get("practice_address", {})
        address = ", ".join(x for x in [addr.get("line1"), addr.get("city"), addr.get("state"), addr.get("zip")] if x)

        primary       = next((t for t in p.get("taxonomies", []) if t.get("primary")), None)
        taxonomy_code = primary.get("code", "") if primary else ""

        providers.append({"name": name, "npi": p.get("npi", ""), "taxonomy_code": taxonomy_code, "address": address})

    return {"supported": True, "state": state_upper, "specialty_searched": specialty_query, "count": len(providers), "providers": providers}


def search_clinical_trials(condition: str, location: str = "", max_results: int = 5) -> dict:
    params = {
        "query.cond": condition,
        "filter.overallStatus": "RECRUITING",
        "pageSize": min(int(max_results), 10),
        "format": "json",
    }
    if location:
        params["query.locn"] = location
    try:
        response = requests.get("https://clinicaltrials.gov/api/v2/studies", params=params, timeout=15)
        response.raise_for_status()
        studies = response.json().get("studies", [])
    except Exception as exc:
        return {"error": f"ClinicalTrials.gov search failed: {exc}"}

    if not studies:
        return {"trials": [], "message": "No recruiting trials found for this condition."}

    trials = []
    for study in studies:
        ps           = study.get("protocolSection", {})
        id_mod       = ps.get("identificationModule", {})
        status_mod   = ps.get("statusModule", {})
        desc_mod     = ps.get("descriptionModule", {})
        elig_mod     = ps.get("eligibilityModule", {})
        contacts_mod = ps.get("contactsLocationsModule", {})
        design_mod   = ps.get("designModule", {})
        nct_id       = id_mod.get("nctId", "")
        raw_locs     = contacts_mod.get("locations", [])
        location_strs = [
            ", ".join(filter(None, [loc.get("facility"), loc.get("city"), loc.get("state")]))
            for loc in raw_locs[:3]
        ]
        trials.append({
            "nct_id": nct_id,
            "title": id_mod.get("briefTitle", ""),
            "status": status_mod.get("overallStatus", ""),
            "phase": ", ".join(design_mod.get("phases", [])) or "N/A",
            "locations": location_strs or ["See ClinicalTrials.gov"],
            "summary": (desc_mod.get("briefSummary") or "")[:400],
            "eligibility": (elig_mod.get("eligibilityCriteria") or "")[:600],
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
        })

    return {"trials": trials}


def record_user_details(email="", name="Name not provided", notes="not provided", message="",
                        chat_history=None, consent_verbatim=False, consent_summary=None, testdata=False):
    if not email or not str(email).strip():
        return {"recorded": "ok", "note": "Email required but not provided"}
    db = _get_db()
    if db is None:
        push(f"Recording interest from {name} with email {email} (DB unavailable)")
        return {"recorded": "ok", "note": "MongoDB unavailable"}
    reason    = message or notes
    lead_coll = db[f"{_ENV_PREFIX}_AboutUs"]["lead"]
    for doc in lead_coll.find({"email": str(email).strip()}):
        return {"recorded": "ok"}
    push(f"Recording interest from {name} with email {email}: {reason}")
    record = {
        "email": email, "name": name, "notes": notes,
        "reason_for_contact": reason,
        "consent_verbatim": consent_verbatim, "consent_summary": consent_summary,
        "datetime": datetime.now().isoformat(), "testdata": testdata,
    }
    if consent_verbatim:
        record["chat_history"] = chat_history or []
    elif consent_summary:
        summary = _summarize_conversation(chat_history)
        summary_msg = [{"role": "user", "content": summary}]
        deIdentify(summary_msg)
        record["notes"] = summary_msg[0]["content"]
    commitSignificantActivity({"database": "AboutUs", "collection": "lead", "record": record})
    return {"recorded": "ok"}


def _summarize_conversation(chat_history):
    if not chat_history:
        return ""
    client    = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    chat_json = json.dumps([{"role": m.get("role", ""), "content": m.get("content") or ""} for m in chat_history], indent=2)
    response  = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=256,
        messages=[{"role": "user", "content": "Summarize this conversation in 2-3 sentences. Focus on what the user wanted and any key information they shared. Be concise.\n\n" + chat_json}],
    )
    return response.content[0].text.strip()


def deIdentify(argChat_history):
    if not argChat_history:
        return
    client    = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    chat_json = json.dumps([{"role": m.get("role", ""), "content": m.get("content") or ""} for m in argChat_history], indent=2)
    response  = client.messages.create(
        model="claude-haiku-4-5-20251001", max_tokens=4096,
        messages=[{"role": "user", "content": (
            "Deidentify the following chat conversation so it meets HIPAA Safe Harbor requirements for research data.\n"
            "Remove or replace: names, geographic identifiers (except state), dates (except year), phone/fax, email, SSN,\n"
            "medical record numbers, account numbers, license numbers, vehicle identifiers, device identifiers, URLs, IP addresses,\n"
            "and any other identifiers that could be used to identify an individual.\n"
            "Preserve the semantic meaning of each message for research purposes.\n\n"
            "Return ONLY a valid JSON array of strings, one string per message in the same order.\n\n"
            f"Chat conversation:\n{chat_json}"
        )}],
    )
    result_text = response.content[0].text.strip()
    if result_text.startswith("```"):
        result_text = result_text.split("```")[1]
        if result_text.startswith("json"):
            result_text = result_text[4:]
        result_text = result_text.strip()
    deidentified_contents = json.loads(result_text)
    for i, content in enumerate(deidentified_contents):
        if i < len(argChat_history):
            argChat_history[i]["content"] = content


def record_unknown_question(question, chat_history=None):
    if chat_history is not None:
        deIdentify(chat_history)
    push(f"Recording a user question I could not answer: {question}")
    commitSignificantActivity({
        "database": "AboutUs", "collection": "AboutSkip",
        "record": {"question": question, "chat_history": chat_history or []}
    })
    return {"recorded": "ok"}


# ---------------------------------------------------------------------------
# Tool definitions — Anthropic format
# ---------------------------------------------------------------------------
anthropic_tools = [
    {
        "name": "find_providers",
        "description": (
            "Search for healthcare providers (doctors, specialists) in a specific US state. "
            "FindCare currently covers Delaware (DE) and Mississippi (MS). "
            "Call this when the user asks to find a doctor, specialist, or provider in a location. "
            "Always confirm the user's state before calling if not provided. "
            "If result contains supported=false, tell the user we're not in their state yet, "
            "ask if they'd like to be notified when we expand, then call record_unknown_question."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "specialty_query": {"type": "string"},
                "state": {"type": "string"},
                "city": {"type": "string"},
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
            "Call this tool BEFORE composing your response whenever ANY of the following is true: "
            "(1) The answer is not explicitly stated in the provided documents. "
            "(2) You would use any hedging word such as 'I think', 'probably', 'might'. "
            "(3) The question is personal medical advice. "
            "Do NOT apply to healthcare navigation questions."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"question": {"type": "string"}},
            "required": ["question"],
        },
    },
    {
        "name": "commitSignificantActivity",
        "description": "Record any custom activity to the database.",
        "input_schema": {
            "type": "object",
            "properties": {
                "database": {"type": "string"},
                "collection": {"type": "string"},
                "record": {"type": "object"},
            },
            "required": ["database", "collection", "record"],
        },
    },
    {
        "name": "find_specialty_codes",
        "description": (
            "Look up NUCC provider taxonomy codes matching a medical specialty or provider type. "
            "Call this when the user asks about a type of doctor, specialist, or medical provider. "
            "If the result contains 'debug': true, display the full JSON result verbatim."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_clinical_trials",
        "description": (
            "Search for actively recruiting clinical trials on ClinicalTrials.gov. "
            "Call this when the user asks to find trials, studies, or research programs."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "location": {"type": "string"},
                "max_results": {"type": "integer"},
            },
            "required": ["condition"],
        },
    },
    {
        "name": "get_skip_snow_context",
        "description": (
            "Returns Skip Snow's personal background: career summary and LinkedIn profile. "
            "Call this when the user asks about Skip Snow specifically — his career, experience, "
            "background, qualifications, or personal story."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_chathealthy_context",
        "description": (
            "Returns ChatHealthy.AI company context: business plan and Anthropic operating principles. "
            "Call this when the user asks about ChatHealthy — the company, the mission, the platform, "
            "the product, the business model, or the Anthropic partnership."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]

# ---------------------------------------------------------------------------
# Me — loads context documents at startup
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
    # Anthropic YouTube transcript excluded — 26k tokens, exceeds rate limit
    # Replaced with compact operating principles (~500 tokens)
    try:
        with open(os.path.join(_ME_DIR, "anthropic_principles.txt"), "r", encoding="utf-8") as f:
            ctx["anthropic_principles"] = f.read()
    except Exception as e:
        ctx["anthropic_principles"] = ""
        _log.warning("ME_DIR anthropic principles load failed: %s", e)
    try:
        with open(os.path.join(_ME_DIR, "summary.txt"), "r", encoding="utf-8") as f:
            ctx["summary"] = f.read()
    except Exception as e:
        ctx["summary"] = ""
        _log.warning("ME_DIR summary load failed: %s", e)
    try:
        reader = PdfReader(os.path.join(_ME_DIR, "chatHealthy_ai_business_plan.pdf"))
        ctx["business_plan"] = "".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        ctx["business_plan"] = ""
        _log.warning("ME_DIR business plan load failed: %s", e)
    try:
        with open(os.path.join(_ME_DIR, "findcare-code-package.json"), "r", encoding="utf-8") as f:
            ctx["codebase"] = f.read()
    except Exception as e:
        ctx["codebase"] = ""
        _log.warning("ME_DIR codebase load failed: %s", e)
    return ctx


_ME = _load_me_context()

# Build number — injected by CI into build.txt at deploy time
_build_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "build.txt")
_BUILD = open(_build_file).read().strip() if os.path.exists(_build_file) else "dev"

WELCOME_MESSAGE = (
    "**Welcome to ChatHealthy FindCare**\n\n"
    "Here's what I can help you with:\n\n"
    "- **Find a doctor** — search for providers by specialty or condition\n"
    "  - Delaware\n"
    "  - Mississippi\n"
    "- **Identify the right specialty** — not sure what kind of doctor you need? Describe your situation\n"
    "- **Clinical trials** — find recruiting research studies for any condition\n"
    "- **About ChatHealthy** — our mission, team, and platform\n\n"
    "If you think you may be having a medical emergency, tell me right away.\n\n"
    "**What can I help you with today?**"
)


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
        f"Answer ONLY from what those tools return. Never use general training knowledge to answer.\n"
        f"RULE 2 — NO HEDGING: You are PROHIBITED from using any hedging language. "
        f"If you would reach for hedging words, call record_unknown_question instead.\n"
        f"RULE 3 — MEDICAL ADVICE vs HEALTHCARE NAVIGATION: "
        f"DECLINE personal medical advice (call record_unknown_question first). "
        f"ALLOWED: Healthcare navigation — use find_providers, find_specialty_codes, search_clinical_trials.\n"
        f"RULE 4 — TOOL CALL ORDER: Always call record_unknown_question BEFORE composing your response when the answer is not known.\n"
        f"RULE 5 — EACH QUESTION SEPARATELY: Record each unknown question with a separate tool call.\n"
        f"RULE 6 — FOLLOW-UP OFFER: When you receive a FOLLOW-UP CHECK reminder, assess genuine interest "
        f"and ask: 'Would you like someone from the ChatHealthy.AI team to follow up with you personally?' "
        f"Respect refusal signals — do not ask again if the user shows impatience or annoyance.\n"
        f"Please chat with the user, always staying in character as {name}."
    )
    if follow_up_check:
        base += (
            "\n\nFOLLOW-UP CHECK: Review the conversation. If the user has shown genuine interest "
            "in a specific topic and you do not yet have their contact details, ask now: "
            "'Would you like someone from the ChatHealthy.AI team to follow up with you personally?'"
        )
    return base


# ---------------------------------------------------------------------------
# Tool execution — Anthropic tool_use blocks
# ---------------------------------------------------------------------------
def _trim(text: str, max_chars: int) -> str:
    """Trim text to max_chars, appending a truncation note if cut."""
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n[... truncated at {max_chars} chars for token budget ...]"


def get_skip_snow_context():
    """Return Skip Snow's personal background: career summary and LinkedIn profile."""
    return {
        "summary": _ME.get("summary", ""),
        "linkedin": _trim(_ME.get("linkedin", ""), 3000),
    }


def get_chathealthy_context():
    """Return ChatHealthy.AI company context: business plan and Anthropic operating principles."""
    return {
        "business_plan": _trim(_ME.get("business_plan", ""), 3000),
        "anthropic_principles": _ME.get("anthropic_principles", ""),
    }


def _handle_tool_calls(tool_use_blocks, messages):
    chat_history = _format_chat_history(messages)
    tool_results = []
    for block in tool_use_blocks:
        name      = block.name
        arguments = dict(block.input)  # already a dict
        if name in ("record_user_details", "record_unknown_question"):
            arguments["chat_history"] = chat_history
        _log.info("Tool called: %s", name)
        fn     = globals().get(name)
        result = fn(**arguments) if fn else {}
        tool_results.append({
            "type": "tool_result",
            "tool_use_id": block.id,
            "content": json.dumps(result),
        })
    return tool_results


# ---------------------------------------------------------------------------
# Debug logging (dev environment only)
# ---------------------------------------------------------------------------
def _debug_log_chat(
    ip: str,
    message: str,
    history_len: int,
    tool_loop_iters: int,
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    response_text: Optional[str],
    error: Optional[str],
) -> None:
    """Persists chat call metadata to dev_Debug.chat_calls when ENV_PREFIX == dev."""
    if _ENV_PREFIX != "dev":
        return
    db = _get_db()
    if db is None:
        return
    try:
        col = db["dev_Debug"]["chat_calls"]
        col.insert_one({
            "datetime":        datetime.now(timezone.utc).isoformat(),
            "ip":              ip,
            "message_preview": message[:200],
            "history_len":     history_len,
            "tool_loop_iters": tool_loop_iters,
            "tokens_in":       tokens_in,
            "tokens_out":      tokens_out,
            "response_preview": response_text[:200] if response_text else None,
            "error":           error,
        })
    except Exception as exc:
        _log.warning("debug_log_chat failed: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="ChatHealthy FindCare API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://chathealthy.ai",
        "https://www.chathealthy.ai",
        "https://dev.chathealthy.ai",
        "http://localhost:3000",
        "http://localhost:5173",
        "http://localhost:8000",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []


class ChatResponse(BaseModel):
    response: Optional[str] = None
    emergency: bool = False
    error: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


@app.get("/welcome")
def welcome():
    return {"message": WELCOME_MESSAGE}


@app.get("/health")
def health():
    db_ok = _get_db() is not None
    return {"status": "ok", "db": "connected" if db_ok else "unavailable", "env": _ENV_PREFIX, "build": _BUILD}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    ip = request.client.host if request.client else "unknown"
    try:
        return await _chat_inner(body, request)
    except Exception as e:
        tb = traceback.format_exc()
        _log.error("CHAT ERROR: %s\n%s", e, tb)
        err_str = str(e)
        # Surface rate-limit token info when available
        if "429" in err_str or "rate_limit" in err_str.lower():
            err_msg = f"Rate limit hit — {err_str}"
        else:
            err_msg = tb if _DEBUG else err_str
        _debug_log_chat(ip, body.message, len(body.history), 0, None, None, None, err_msg)
        return ChatResponse(error=err_msg)


async def _chat_inner(body: ChatRequest, request: Request):
    ip = request.client.host if request.client else "unknown"

    if _admin_unlock(body.message, ip):
        return ChatResponse(response="Session unlocked.")

    history = body.history

    if _session_is_locked(history):
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    if _check_ip_lock_db(ip) or _safety_check(body.message):
        _lock_ip_db(ip, trigger_message=body.message, history=history)
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    user_msg_count = sum(1 for m in history if m.get("role") == "user")
    follow_up      = user_msg_count > 0 and user_msg_count % 5 == 0
    system         = _system_prompt(follow_up_check=follow_up)

    # Build messages: history + new user message
    messages = list(history) + [{"role": "user", "content": body.message}]

    anthropic_client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))

    total_tokens_in  = 0
    total_tokens_out = 0

    _log.info("CHAT call=initial msgs=%d", len(messages))
    response = anthropic_client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=system,
        messages=messages,
        tools=anthropic_tools,
    )
    total_tokens_in  += getattr(response.usage, "input_tokens",  0)
    total_tokens_out += getattr(response.usage, "output_tokens", 0)

    # Tool-use loop
    loop_iter = 0
    while response.stop_reason == "tool_use":
        tool_uses    = [b for b in response.content if b.type == "tool_use"]
        tool_names   = [b.name for b in tool_uses]
        tool_results = _handle_tool_calls(tool_uses, messages)

        # Append assistant turn (with tool_use blocks) + tool results as user turn
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user",      "content": tool_results})

        loop_iter += 1
        _log.info("CHAT call=tool_loop iter=%d tools=%s msgs=%d tokens_in=%d", loop_iter, tool_names, len(messages), total_tokens_in)
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            system=system,
            messages=messages,
            tools=anthropic_tools,
        )
        total_tokens_in  += getattr(response.usage, "input_tokens",  0)
        total_tokens_out += getattr(response.usage, "output_tokens", 0)

    text = next((b.text for b in response.content if b.type == "text"), "")

    _log.info("CHAT complete tokens_in=%d tokens_out=%d", total_tokens_in, total_tokens_out)
    _debug_log_chat(ip, body.message, len(history), loop_iter, total_tokens_in, total_tokens_out, text, None)
    return ChatResponse(response=text, tokens_in=total_tokens_in, tokens_out=total_tokens_out)


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
