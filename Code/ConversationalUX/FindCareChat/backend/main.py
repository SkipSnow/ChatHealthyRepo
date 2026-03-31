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
from url_guardian import URLGuardian
from application.tool_router import ToolRouter
from application.facades.find_care_facade import FindCareFacade
from domain.find_care.provider_search_service import ProviderSearchService
from domain.find_care.specialty_service import SpecialtyService
from application.facades.evaluate_care_facade import EvaluateCareFacade
from domain.evaluate_care_quality.clinical_trials_service import ClinicalTrialsService
from domain.evaluate_care_quality.provider_detail_service import ProviderDetailService

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
SUPPORTED_STATES = {"DE", "MS", "VA"}
_ENV_PREFIX       = os.getenv("ENV_PREFIX", "dev")
_DEBUG            = os.getenv("DEBUG", "false").lower() == "true"
_HUMAN_TESTING    = os.getenv("HUMAN_TESTING", "false").lower() == "true"
_APP_VERSION      = os.getenv("APP_VERSION", "unknown")
# TODO: when ENV_PREFIX routing is implemented, override test mode in production:
# if _ENV_PREFIX == "prod":
#     _HUMAN_TESTING = False

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
    _log.info("SAFETY: _lock_ip_db called for %s, collection=%s", ip, col is not None)
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
    deIdentify(safe_history)
    try:
        col.update_one(
            {"ip": ip},
            {"$set": {
                "locked_at": now.isoformat(),
                "expires_at": expires.isoformat(),
                "trigger_message": trigger_message[:500],
                "chat_history_deidentified": safe_history,
            }},
            upsert=True,
        )
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
        record = col.find_one({"ip": ip, "expires_at": {"$gt": now_iso}, "unlocked": {"$ne": True}})
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
    if message.strip().upper() == _ADMIN_UNLOCK_KEY.upper() or message.strip().upper() == f"UNLOCK:{_ADMIN_UNLOCK_KEY.upper()}":
        _ip_emergency_locks.pop(ip, None)
        col = _safety_collection()
        if col is not None:
            try:
                col.update_many(
                    {"ip": ip},
                    {"$set": {
                        "unlocked": True,
                        "unlocked_at": datetime.now(timezone.utc).isoformat(),
                        "unlocked_by": "admin",
                    }},
                )
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

    # Fast path: skip safety check for obvious non-emergency patterns
    _SAFE_PREFIXES = [
        "can you tell me more about", "tell me about", "more info on",
        "what can you tell me about", "who is", "find me", "search for",
        "look up", "what is", "how do i", "where can i",
    ]
    if any(msg_lower.startswith(p) for p in _SAFE_PREFIXES):
        return False

    keyword_hit = any(kw in msg_lower for kw in EMERGENCY_KEYWORDS)
    ai_hit = False
    try:
        oai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = oai.chat.completions.create(
            model="gpt-4.1-mini",
            max_tokens=64,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": (
                    "You are a safety classifier. Decide ONLY between two outcomes:\n"
                    "  EMERGENCY - call 911 or go to ER immediately\n"
                    "  PROBE - ask a clarifying question first\n\n"
                    "EMERGENCY requires ALL of: (1) specific body location or system named, "
                    "(2) acute onset or severity stated, (3) life-threat implied.\n"
                    "Examples that ARE emergencies: 'chest pain', 'I can't breathe', "
                    "'I think I'm having a heart attack', 'I took too many pills', "
                    "'I want to kill myself', 'I'm having a seizure'.\n\n"
                    "NEVER EMERGENCY - these are ALWAYS PROBE:\n"
                    "  'I'm in pain' - location unknown -> PROBE\n"
                    "  'I'm in a lot of pain' - location unknown -> PROBE\n"
                    "  'I don't feel well' -> PROBE\n"
                    "  'something hurts' -> PROBE\n"
                    "  'I feel sick' -> PROBE\n"
                    "  ANY pain without specific body location and stated severity -> PROBE\n"
                    "  ANY question about a doctor, provider, or person -> PROBE\n"
                    "  ANY request for information or search -> PROBE\n"
                    "  ANY message containing 'tell me about', 'more info', 'find me' -> PROBE\n\n"
                    "Return ONLY valid JSON: {\"emergency\": true|false, \"confidence\": 0.0-1.0}"
                )},
                {"role": "user", "content": message},
            ],
        )
        raw = resp.choices[0].message.content.strip()
        result = json.loads(raw)
        confidence = float(result.get("confidence", 0))
        ai_hit = bool(result.get("emergency", False)) and confidence >= 0.80
    except Exception as exc:
        _log.error("Safety check AI failed (%s) — falling back to keyword-only", exc)
        # Don't escalate on AI failure — use keyword match only
        return keyword_hit
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
    try:
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
    except Exception as exc:
        _log.error("commitSignificantActivity failed: %s", exc)
        return {"recorded": "error", "note": str(exc)}


def _format_chat_history(messages, truncate: bool = True):
    """Format chat history. truncate=True for safety/debug (500 chars), False for verbatim consent."""
    max_len = 500 if truncate else None
    out = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        c = m.get("content")
        if isinstance(c, str):
            out.append({"role": m["role"], "content": c[:max_len] if max_len else c})
        elif isinstance(c, list):
            # Anthropic content blocks — extract text
            text = " ".join(b.get("text", "") if isinstance(b, dict) else getattr(b, "text", "") for b in c)
            out.append({"role": m["role"], "content": text[:max_len] if max_len else text})
    return out


# ---------------------------------------------------------------------------
# Provider tools
# ---------------------------------------------------------------------------
def _expand_query_terms(query: str) -> list[str]:
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
        _log.warning("_expand_query_terms failed for %r: %s — returning empty list", query, exc)
        return []


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
        try:
            regex_codes, stems = rf.result()
        except Exception as exc:
            _log.warning("regex_pipeline failed: %s", exc)
            regex_codes, stems = [], []
        try:
            vector_codes, classifications = vf.result()
        except Exception as exc:
            _log.warning("vector_pipeline failed: %s", exc)
            vector_codes, classifications = [], []

    seen, all_codes = set(), []
    for doc in vector_codes + regex_codes:
        if doc["Code"] not in seen:
            seen.add(doc["Code"])
            all_codes.append(doc)

    # Cap results — full NUCC dumps can exceed token budget in the tool loop
    # 70 covers broad specialties like pediatrics (~60 sub-types)
    all_codes = all_codes[:70]

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


# HACK ASN-4AFBDA: pipeline bug leaves county.fips set but county.name blank for 2,579
# providers (460 DE, 2,119 MS). Build FIPS→name from providers that already have both
# fields and use it to fill gaps at query time. Remove once pipeline fix is deployed.
# Deliverable: fix before end of next iteration.

# Static DE + MS county FIPS table — authoritative seed so queries work even when
# all records in a county lack county.name (worst case of ASN-4AFBDA).
_FIPS_STATIC: dict[str, str] = {
    # Delaware (3 counties)
    "10001": "Kent", "10003": "New Castle", "10005": "Sussex",
    # Mississippi (82 counties)
    "28001": "Adams", "28003": "Alcorn", "28005": "Amite", "28007": "Attala",
    "28009": "Benton", "28011": "Bolivar", "28013": "Calhoun", "28015": "Carroll",
    "28017": "Chickasaw", "28019": "Choctaw", "28021": "Claiborne", "28023": "Clarke",
    "28025": "Clay", "28027": "Coahoma", "28029": "Copiah", "28031": "Covington",
    "28033": "DeSoto", "28035": "Forrest", "28037": "Franklin", "28039": "George",
    "28041": "Greene", "28043": "Grenada", "28045": "Hancock", "28047": "Harrison",
    "28049": "Hinds", "28051": "Holmes", "28053": "Humphreys", "28055": "Issaquena",
    "28057": "Itawamba", "28059": "Jackson", "28061": "Jasper", "28063": "Jefferson",
    "28065": "Jefferson Davis", "28067": "Jones", "28069": "Kemper", "28071": "Lafayette",
    "28073": "Lamar", "28075": "Lauderdale", "28077": "Lawrence", "28079": "Leake",
    "28081": "Lee", "28083": "Leflore", "28085": "Lincoln", "28087": "Lowndes",
    "28089": "Madison", "28091": "Marion", "28093": "Marshall", "28095": "Monroe",
    "28097": "Montgomery", "28099": "Neshoba", "28101": "Newton", "28103": "Noxubee",
    "28105": "Oktibbeha", "28107": "Panola", "28109": "Pearl River", "28111": "Perry",
    "28113": "Pike", "28115": "Pontotoc", "28117": "Prentiss", "28119": "Quitman",
    "28121": "Rankin", "28123": "Scott", "28125": "Sharkey", "28127": "Simpson",
    "28129": "Smith", "28131": "Stone", "28133": "Sunflower", "28135": "Tallahatchie",
    "28137": "Tate", "28139": "Tippah", "28141": "Tishomingo", "28143": "Tunica",
    "28145": "Union", "28147": "Walthall", "28149": "Warren", "28151": "Washington",
    "28153": "Wayne", "28155": "Webster", "28157": "Wilkinson", "28159": "Winston",
    "28161": "Yalobusha", "28163": "Yazoo",
}

_fips_to_county: dict[str, str] = dict(_FIPS_STATIC)  # pre-seeded; DB values override on load


def _load_fips_county_map() -> None:
    global _fips_to_county
    db = _get_db()
    if db is None:
        return
    try:
        pairs = db[f"{_ENV_PREFIX}_PublicHealthData"]["providers"].aggregate([
            {"$match": {"county.fips": {"$exists": True}, "county.name": {"$exists": True}}},
            {"$group": {"_id": "$county.fips", "name": {"$first": "$county.name"}}},
        ])
        db_map = {p["_id"]: p["name"] for p in pairs}
        _fips_to_county.update(db_map)  # DB values override static seed
        _log.info("HACK ASN-4AFBDA: loaded %d FIPS→county mappings (%d from DB)", len(_fips_to_county), len(db_map))
    except Exception as exc:
        _log.warning("HACK ASN-4AFBDA: failed to load FIPS map: %s", exc)


def _make_county_filter(county: str) -> dict:
    """Build a DB filter for county matching by name OR fips (handles ASN-4AFBDA records)."""
    term = county.strip().lower()
    name_filter: dict = {"county.name": {"$regex": county.strip(), "$options": "i"}}
    matching_fips = [fips for fips, name in _fips_to_county.items() if term in name.lower()]
    if matching_fips:
        return {"$or": [name_filter, {"county.fips": {"$in": matching_fips}}]}
    return name_filter


_oai_client: Optional[OpenAI] = None


def _get_oai_client() -> OpenAI:
    global _oai_client
    if _oai_client is None:
        _oai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _oai_client


def _get_query_embedding(text: str) -> Optional[list]:
    try:
        resp = _get_oai_client().embeddings.create(model="text-embedding-3-large", input=text)
        return resp.data[0].embedding
    except Exception as e:
        _log.warning("Embedding query failed: %s", e)
        return None


def _format_provider(p: dict) -> dict:
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
    primary = next((t for t in p.get("taxonomies", []) if t.get("primary")), None)
    county_obj = p.get("county", {})
    county_name = county_obj.get("name") or _fips_to_county.get(county_obj.get("fips", ""), "")  # HACK ASN-4AFBDA
    raw_phone = addr.get("phone", "")
    phone = f"({raw_phone[:3]}) {raw_phone[3:6]}-{raw_phone[6:]}" if len(raw_phone) == 10 else raw_phone
    return {
        "name": name,
        "npi": p.get("npi", ""),
        "taxonomy_code": primary.get("code", "") if primary else "",
        "address": address,
        "phone": phone,
        "county": county_name,
    }


_PROVIDER_PROJECTION = {
    "_id": 0, "npi": 1, "entity_type_code": 1,
    "provider_first_name": 1, "provider_last_name_legal_name": 1,
    "provider_middle_name": 1, "provider_name_prefix_text": 1,
    "provider_name_suffix_text": 1, "provider_credential_text": 1,
    "provider_organization_name_legal_business_name": 1,
    "practice_address": 1, "taxonomies": 1, "county": 1,
}


def _vector_search_providers(embedding: list, state: str, city: str, county: str, limit: int) -> list:
    db = _get_db()
    if db is None:
        return []
    pipeline = [
        {
            "$vectorSearch": {
                "index": "provider_vector_index",
                "path": "embedding",
                "queryVector": embedding,
                "numCandidates": min(limit * 30, 300),
                "limit": min(limit * 6, 60),
                "filter": {"practice_address.state": state},
            }
        },
        {"$match": {"practice_address.state": state}},
    ]
    if city:
        pipeline.append({"$match": {"practice_address.city": {"$regex": city.strip(), "$options": "i"}}})
    if county:
        pipeline.append({"$match": _make_county_filter(county)})
    pipeline += [{"$limit": limit}, {"$project": _PROVIDER_PROJECTION}]
    try:
        raw = list(db[f"{_ENV_PREFIX}_PublicHealthData"]["providers"].aggregate(pipeline))
        return [_format_provider(p) for p in raw]
    except Exception as e:
        _log.warning("Vector search failed: %s", e)
        return []


def find_providers(specialty_query: str, state: str, city: str = "", county: str = "", limit: int = 5) -> dict:
    state_upper = state.upper().strip()
    if state_upper not in SUPPORTED_STATES:
        return {
            "supported": False,
            "state": state_upper,
            "message": (
                f"FindCare is currently available in Delaware (DE), Mississippi (MS), and Virginia (VA) only. "
                f"We've noted interest in {state_upper}."
            ),
        }

    db = _get_db()
    if db is None:
        return {"error": "Database unavailable"}

    safe_limit = min(int(limit), 10)

    # --- Vector search path ---
    embedding = _get_query_embedding(specialty_query)
    if embedding:
        providers = _vector_search_providers(embedding, state_upper, city, county, safe_limit)
        if providers:
            _log.info("find_providers: vector search returned %d results for '%s' in %s", len(providers), specialty_query, state_upper)
            return {"supported": True, "state": state_upper, "specialty_searched": specialty_query, "search_mode": "vector", "count": len(providers), "providers": providers}
        _log.info("find_providers: vector search returned 0 results, falling back to taxonomy search")

    # --- Taxonomy code fallback ---
    specialty_result = find_specialty_codes(specialty_query)
    if "error" in specialty_result:
        return specialty_result

    codes = [s["Code"] for s in specialty_result.get("specialties", [])]
    if not codes:
        return {"supported": True, "providers": [], "message": f"No matching specialty found for '{specialty_query}'."}

    query_filter: dict = {
        "practice_address.state": state_upper,
        "taxonomies.code": {"$in": codes},
    }
    if city:
        query_filter["practice_address.city"] = {"$regex": city.strip(), "$options": "i"}
    if county:
        query_filter.update(_make_county_filter(county))

    raw = list(
        db[f"{_ENV_PREFIX}_PublicHealthData"]["providers"]
        .find(query_filter, _PROVIDER_PROJECTION)
        .limit(safe_limit)
    )

    if raw:
        providers = [_format_provider(p) for p in raw]
        _log.info("find_providers: taxonomy search returned %d results for '%s' in %s", len(providers), specialty_query, state_upper)
        return {"supported": True, "state": state_upper, "specialty_searched": specialty_query, "search_mode": "taxonomy", "count": len(providers), "providers": providers}

    # --- County fallback: specialty codes didn't match; broaden to all individual providers ---
    # Handles general queries like "doctors in X county" where taxonomy codes are too specific.
    # Filters to individual providers (entity_type_code=1) whose primary taxonomy starts with "2"
    # (NUCC physician block) to avoid returning pharmacies, DME, etc.
    if county:
        county_filter: dict = {
            "practice_address.state": state_upper,
            "entity_type_code": "1",
            "taxonomies": {"$elemMatch": {"code": {"$regex": "^2"}, "primary": True}},
        }
        county_filter.update(_make_county_filter(county))
        raw = list(
            db[f"{_ENV_PREFIX}_PublicHealthData"]["providers"]
            .find(county_filter, _PROVIDER_PROJECTION)
            .limit(safe_limit)
        )
        if raw:
            providers = [_format_provider(p) for p in raw]
            _log.info("find_providers: county physician fallback returned %d for county '%s' in %s", len(providers), county, state_upper)
            return {"supported": True, "state": state_upper, "county_searched": county, "search_mode": "county_physicians", "count": len(providers), "providers": providers}

    return {"supported": True, "providers": [], "message": f"No {specialty_query} providers found in {state_upper}."}


def _get_travel_info(origin: str, destinations: list[str]) -> dict[str, dict]:
    """Call Google Routes API to get drive distance and time for each destination.
    Returns {destination: {"distance": "X miles", "duration": "Xh Ym"}} or empty on failure."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key or not destinations:
        return {}
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration",
        "Content-Type": "application/json",
    }
    results = {}
    for dest in destinations:
        try:
            body = {
                "origin": {"address": origin},
                "destination": {"address": dest},
                "travelMode": "DRIVE",
                "routingPreference": "TRAFFIC_UNAWARE",
            }
            resp = requests.post(
                "https://routes.googleapis.com/directions/v2:computeRoutes",
                json=body, headers=headers, timeout=10,
            )
            if resp.status_code != 200:
                continue
            data = resp.json()
            routes = data.get("routes", [])
            if not routes:
                continue
            r = routes[0]
            meters = r.get("distanceMeters", 0)
            miles = meters / 1609.34
            secs = int(r.get("duration", "0s").replace("s", ""))
            hrs = secs // 3600
            mins = (secs % 3600) // 60
            results[dest] = {
                "distance": f"{miles:.0f} miles",
                "duration": f"{hrs}h {mins}m" if hrs else f"{mins}m",
            }
        except Exception as exc:
            _log.debug("Routes API failed for %s: %s", dest, exc)
    return results


def search_clinical_trials(condition: str, location: str = "", user_location: str = "", max_results: int = 5) -> dict:
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
    # Collect unique location city/states for distance calculation
    travel_destinations = []
    trial_location_map = {}  # maps trial index -> list of "city, state" strings

    for idx, study in enumerate(studies):
        ps           = study.get("protocolSection", {})
        id_mod       = ps.get("identificationModule", {})
        status_mod   = ps.get("statusModule", {})
        desc_mod     = ps.get("descriptionModule", {})
        elig_mod     = ps.get("eligibilityModule", {})
        contacts_mod = ps.get("contactsLocationsModule", {})
        design_mod   = ps.get("designModule", {})
        nct_id       = id_mod.get("nctId", "")
        raw_locs     = contacts_mod.get("locations", [])

        # Build location objects with city/state for distance lookup
        locs = []
        for loc in raw_locs[:5]:
            facility = loc.get("facility", "")
            city = loc.get("city", "")
            state = loc.get("state", "")
            loc_str = ", ".join(filter(None, [facility, city, state]))
            city_state = ", ".join(filter(None, [city, state]))
            loc_obj = {"display": loc_str, "city_state": city_state}
            locs.append(loc_obj)
            if city_state and city_state not in travel_destinations:
                travel_destinations.append(city_state)

        trial_location_map[idx] = locs

        trials.append({
            "nct_id": nct_id,
            "title": id_mod.get("briefTitle", ""),
            "status": status_mod.get("overallStatus", ""),
            "phase": ", ".join(design_mod.get("phases", [])) or "N/A",
            "locations": [l["display"] for l in locs] or ["See ClinicalTrials.gov"],
            "summary": (desc_mod.get("briefSummary") or "")[:400],
            "eligibility": (elig_mod.get("eligibilityCriteria") or "")[:600],
            "url": f"https://clinicaltrials.gov/study/{nct_id}",
        })

    # If user provided their location, calculate travel times
    if user_location and travel_destinations:
        travel_info = _get_travel_info(user_location, travel_destinations[:25])  # API limit
        if travel_info:
            for idx, trial in enumerate(trials):
                locs = trial_location_map.get(idx, [])
                travel = []
                for loc in locs:
                    cs = loc["city_state"]
                    if cs in travel_info:
                        travel.append({
                            "location": loc["display"],
                            "distance": travel_info[cs]["distance"],
                            "travel_time": travel_info[cs]["duration"],
                        })
                if travel:
                    trial["travel_info"] = travel

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
    try:
        for doc in lead_coll.find({"email": str(email).strip()}):
            return {"recorded": "ok"}
    except Exception as exc:
        _log.error("record_user_details DB lookup failed: %s", exc)
        return {"recorded": "error", "note": "Database error"}
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
    try:
        client    = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
        chat_json = json.dumps([{"role": m.get("role", ""), "content": m.get("content") or ""} for m in chat_history], indent=2)
        response  = client.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=256,
            messages=[{"role": "user", "content": "Summarize this conversation in 2-3 sentences. Focus on what the user wanted and any key information they shared. Be concise.\n\n" + chat_json}],
        )
        return response.content[0].text.strip()
    except Exception as exc:
        _log.error("_summarize_conversation failed: %s", exc)
        return ""


def deIdentify(argChat_history):
    if not argChat_history:
        return
    try:
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
    except Exception as exc:
        _log.warning("deIdentify failed: %s — keeping original content", exc)


_UNKNOWN_TEMPLATES = {
    "healthcare_capability": (
        'I don\'t have that capability yet. '
        'May I record your question so we can improve? '
        'We would save a de-identified version of this conversation.'
    ),
    "medical_advice": (
        'I am not able to provide medical advice. Please consult your doctor. '
        'May I record your question so we can improve? '
        'We would save a de-identified version of this conversation.'
    ),
    "irrelevant": (
        'That is not something I can help with. '
        'May I record your question so we can improve? '
        'We would save a de-identified version of this conversation.'
    ),
}


def record_unknown_question(question, question_class="irrelevant", consent=False, chat_history=None):
    """Classify an unanswerable question. Only records to DB if consent=True.
    question_class: 'healthcare_capability' | 'medical_advice' | 'irrelevant'"""
    template = _UNKNOWN_TEMPLATES.get(question_class, _UNKNOWN_TEMPLATES["irrelevant"])
    if consent:
        if chat_history is not None:
            deIdentify(chat_history)
        push(f"Recording a user question I could not answer: {question}")
        commitSignificantActivity({
            "database": "AboutUs", "collection": "AboutSkip",
            "record": {
                "question": question,
                "question_class": question_class,
                "chat_history": chat_history or [],
            }
        })
        return {"recorded": "ok", "response_template": "Thank you, your question has been recorded. Would you like someone from the ChatHealthy team to follow up with you on this?"}
    return {"recorded": "pending_consent", "response_template": template, "question_class": question_class,
            "instruction": "Present the template VERBATIM. If user consents to recording, call this tool again with consent=true. If user declines recording, still ask: Would you like someone from ChatHealthy to follow up with you on this?"}


# ---------------------------------------------------------------------------
# Tool definitions — Anthropic format
# ---------------------------------------------------------------------------
anthropic_tools = [
    {
        "name": "find_providers",
        "description": (
            "Search for healthcare providers (doctors, specialists) in a specific US state. "
            "FindCare currently covers Delaware (DE), Mississippi (MS), and Virginia (VA). "
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
            "Classify the question into one of three classes and pass it as question_class. "
            "The tool returns a response_template — you MUST present this template VERBATIM to the user. "
            "Do NOT add to it, rephrase it, or elaborate. Present ONLY the template text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "question": {"type": "string", "description": "The user's question"},
                "question_class": {
                    "type": "string",
                    "enum": ["healthcare_capability", "medical_advice", "irrelevant"],
                    "description": "healthcare_capability: healthcare topic but we lack the feature (insurance, reviews, quality). medical_advice: user seeks personal medical guidance. irrelevant: not related to healthcare or our platform.",
                },
                "consent": {
                    "type": "boolean",
                    "description": "Set to true ONLY after the user explicitly consents to recording their question. First call: omit or false. Second call after consent: true.",
                },
            },
            "required": ["question", "question_class"],
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
            "Call this when the user asks to find trials, studies, or research programs. "
            "On the first call, omit user_location to get initial results. "
            "After showing results, ask the user if they want to know travel distances. "
            "If yes, call again with the same condition + their city/state in user_location "
            "to get distance and estimated drive time to each trial site."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "condition": {"type": "string"},
                "location": {"type": "string", "description": "Filter trials by this location"},
                "user_location": {"type": "string", "description": "User's city and state for travel time calculation (e.g. 'Jackson, MS')"},
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
    {
        "name": "lookup_provider_external",
        "description": (
            "ALWAYS call this tool when the user asks about a specific provider by name — "
            "'tell me about Dr. X', 'what can you tell me about X', 'more info on X', etc. "
            "Returns NPI Registry data (name, specialty, license, address, phone) and research site links. "
            "Present the NPI details, then say: 'You can do more research on these sites:' and list the links. "
            "Ask: 'Would you like guidance on how to use any of these sites?' "
            "If yes, look up the site guidance from the research_sites field and present it."
        ),
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


def lookup_provider_external(provider_name: str, npi: str = "", state: str = "", **kwargs) -> dict:
    """Look up provider details from NPI Registry API and construct search URLs."""
    import urllib.parse
    name_q = urllib.parse.quote_plus(provider_name)

    # NPI Registry API lookup — get real provider data
    npi_data = None
    if npi:
        try:
            npi_resp = requests.get(
                f"https://npiregistry.cms.hhs.gov/api/?number={npi}&version=2.1",
                timeout=10,
            )
            if npi_resp.status_code == 200:
                results = npi_resp.json().get("results", [])
                if results:
                    r = results[0]
                    basic = r.get("basic", {})
                    addrs = [a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"]
                    addr = addrs[0] if addrs else {}
                    taxonomies = r.get("taxonomies", [])
                    primary_tax = next((t for t in taxonomies if t.get("primary")), taxonomies[0] if taxonomies else {})
                    npi_data = {
                        "name": " ".join(filter(None, [basic.get("name_prefix", ""), basic.get("first_name", ""), basic.get("middle_name", ""), basic.get("last_name", "")])),
                        "npi": r.get("number", npi),
                        "status": basic.get("status", ""),
                        "credential": basic.get("credential", ""),
                        "specialty": primary_tax.get("desc", ""),
                        "license": primary_tax.get("license", ""),
                        "license_state": primary_tax.get("state", ""),
                        "address": ", ".join(filter(None, [addr.get("address_1", ""), addr.get("city", ""), addr.get("state", ""), addr.get("postal_code", "")[:5] if addr.get("postal_code") else ""])),
                        "phone": addr.get("telephone_number", ""),
                        "enumeration_date": basic.get("enumeration_date", ""),
                    }
        except Exception as exc:
            _log.warning("NPI Registry API lookup failed for %s: %s", npi, exc)

    links = {
        # Zocdoc removed — blocks all third-party links (403)
        "healthgrades": f"https://www.healthgrades.com/find-a-doctor?what={name_q}&where={state}",
        "npi_registry": f"https://npiregistry.cms.hhs.gov/search?number={npi}" if npi else f"https://npiregistry.cms.hhs.gov/search?name_type=ind&first_name={name_q}",
    }
    # State medical board links for supported states
    state_boards = {
        "DE": "https://dpr.delaware.gov/boardsearch/",
        "MS": "https://www.msbml.ms.gov/licensure",
        "VA": "https://dhp.virginiainteractive.org/Lookup/Index",
    }
    if state.upper() in state_boards:
        links["state_medical_board"] = state_boards[state.upper()]

    research_sites = {
        "healthgrades": {
            "url": f"https://www.healthgrades.com/find-a-doctor?what={name_q}&where={state}",
            "name": "Healthgrades",
            "guidance": "Search by the provider's full name and state. Note: Healthgrades uses fuzzy matching and may show similar names — verify the provider name and address match before relying on reviews."
        },
        "npi_registry": {
            "url": f"https://npiregistry.cms.hhs.gov/search?number={npi}" if npi else f"https://npiregistry.cms.hhs.gov/search?name_type=ind&first_name={name_q}",
            "name": "NPI Registry (CMS)",
            "guidance": "The official federal registry. Search by NPI number for exact match. Shows license state, specialty, and practice address. This is the most reliable source for verifying a provider's credentials."
        },
    }
    if state.upper() in state_boards:
        board = state_boards[state.upper()]
        state_name = {"DE": "Delaware", "MS": "Mississippi", "VA": "Virginia"}.get(state.upper(), state)
        research_sites["state_medical_board"] = {
            "url": board,
            "name": f"{state_name} Medical Board",
            "guidance": f"Search the {state_name} state medical board to verify active licensure, check for disciplinary actions, and confirm board certification."
        }

    result = {"provider_name": provider_name, "npi": npi, "research_sites": research_sites}
    if npi_data:
        result["npi_details"] = npi_data
    return result


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
_load_fips_county_map()  # HACK ASN-4AFBDA
_url_guardian = URLGuardian(cache_ttl=3600, request_timeout=5)

# Build number — read from MongoDB once at startup. Incremented by ops/bump_build.py only.
# Every deploy restarts the app, so the number updates naturally.
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

WELCOME_MESSAGE = (
    "**Welcome to ChatHealthy FindCare**\n\n"
    "Here's what I can help you with:\n\n"
    "- **Find a doctor** — search for providers by specialty or condition\n"
    "  - Delaware\n"
    "  - Mississippi\n"
    "  - Virginia\n"
    "- **Get provider details** — credentials, license, NPI data, and research links\n"
    "- **Identify the right specialty** — not sure what kind of doctor you need? Describe your situation\n"
    "- **Clinical trials** — find recruiting research studies for any condition\n"
    "  - Find distance and travel time from any location to trial sites\n"
    "- **About ChatHealthy** — our mission, team, and platform\n"
    "- **Contact us** — request a follow-up from the ChatHealthy team\n"
    "- **Ask anything** — if I can't answer, I'll record your question and offer to connect you with someone who can\n"
    "- **Emergency detection** — if you appear to need emergency care, the chat will lock and direct you to call 911, with a full audit trail\n"
    "- **HIPAA-like consent** — we seek your consent before storing any identified or de-identified user data\n\n"
    "If you think you may be having a medical emergency, tell me right away — I'll lock the chat for an hour and insist you seek ER care.\n\n"
    "**What can I help you with today?**"
)

_TEST_START_BUILD = 100
_TEST_FEATURES = [
    {"id": 1,  "feature": "Provider Search (DE + MS, vector + regex)",     "completed": True, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 0, "features_deferred": 0,
     "bugs": [], "enhancements": []},
    {"id": 2,  "feature": "Specialty Identification (NUCC + AI expansion)","completed": False, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 0, "features_deferred": 1,
     "bugs": [], "enhancements": [],
     "defer_reason": "Sufficiently working for release. Deep testing deferred - too complex for alpha."},
    {"id": 3,  "feature": "Clinical Trials Search",                        "completed": True, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 2, "features_deferred": 0,
     "bugs": [],
     "enhancements": ["Travel distance + drive time via Google Routes API", "Two-pass UX: results first, travel on request"]},
    {"id": 4,  "feature": "About ChatHealthy / Skip Snow",                 "completed": True, "bugs_fixed": 0, "bugs_deferred": 1, "features_implemented": 0, "features_deferred": 0,
     "bugs": ["Sonnet answers questions outside context instead of saying I don't know. Fix: PreScreen class (Sprint 2)"],
     "enhancements": []},
    {"id": 5,  "feature": "Safety Filter (dual-trigger, IP lock, audit)",  "completed": True, "bugs_fixed": 3, "bugs_deferred": 0, "features_implemented": 2, "features_deferred": 0,
     "bugs": ["False positive on provider name lookup (safe-prefix bypass added)", "Unlock code visible in audit record (deIdentify added)", "Triggering message missing from audit history (append current msg)"],
     "enhancements": ["Switched classifier to GPT-4.1-mini (vendor diversity, 2.5x cheaper)", "Full de-identified conversation history in safety audit trail"]},
    {"id": 6,  "feature": "Lead Capture (follow-up offer)",                "completed": True, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 0, "features_deferred": 0,
     "bugs": [], "enhancements": []},
    {"id": 7,  "feature": "Consent Framework",                             "completed": False, "bugs_fixed": 0, "bugs_deferred": 4, "features_implemented": 0, "features_deferred": 0,
     "bugs": ["Sonnet set consent_verbatim=true when user asked for de-identification", "PII not scrubbed when de-identify was requested", "Exact consent wording needs legal review before beta", "No separation between question consent and contact-me consent"],
     "enhancements": [],
     "fail_reason": "Two consent streams needed: Stream 1 (questions) always de-identify. Stream 2 (contact me) user chooses verbatim or de-identify with PHI warning."},
    {"id": 8,  "feature": "Provider Detail",                               "completed": False, "bugs_fixed": 1, "bugs_deferred": 1, "features_implemented": 1, "features_deferred": 0,
     "bugs": ["Healthgrades fuzzy match returns wrong provider", "Zocdoc blocks all third-party links (403)"],
     "enhancements": ["NPI Registry API lookup for real provider data", "Repositioned links as research sites with user guidance"],
     "fail_reason": "External link quality unreliable. Full fix deferred."},
    {"id": 9,  "feature": "URL Guardian (validate + defang broken links)", "completed": False, "bugs_fixed": 0, "bugs_deferred": 1, "features_implemented": 0, "features_deferred": 0,
     "bugs": [], "enhancements": [],
     "defer_reason": "Link check deferred to next build. V2 design ready (3-stage: HEAD, AI content verify, Google search correction)."},
    {"id": 10, "feature": "Chat UX (timer, stop, markdown, emergency)",    "completed": False, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 0, "features_deferred": 1,
     "bugs": [], "enhancements": [],
     "defer_reason": "Sufficient for v0.1.2. Deep testing deferred."},
    {"id": 11, "feature": "Blob Storage Infrastructure",                   "completed": True, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 2, "features_deferred": 0,
     "bugs": [],
     "enhancements": ["Created admin + dev-brain containers", "Consolidated provider-data into chathealthy-public-data"]},
    {"id": 12, "feature": "Unanswerable Question Handling",                "completed": True, "bugs_fixed": 3, "bugs_deferred": 0, "features_implemented": 4, "features_deferred": 0,
     "bugs": ["System answered unanswerable questions without recording (fixed: RULE 2 source-bounded knowledge)", "Responses too verbose (fixed: template responses from tool)", "Mid-session context caused rule bypass (fixed: evaluate each message independently)"],
     "enhancements": ["3-path classification (healthcare capability / medical advice / irrelevant)", "Template responses — verbatim from tool, no Sonnet elaboration", "Consent before recording questions", "Follow-up offer after recording"]},
    {"id": 13, "feature": "Markdown Table Rendering (GFM tables in chat)", "completed": True, "bugs_fixed": 1, "bugs_deferred": 0, "features_implemented": 11, "features_deferred": 0,
     "bugs": ["GFM tables not rendering (fixed: remark-gfm + sanitize whitelist)"],
     "enhancements": ["Bordered table cells", "Repeating header every 7 rows", "Notes section", "DEF/FAIL status in Done column", "Build number auto-increment", "Summary counts in header", "Removed deferred columns", "Frontend fetches /welcome from API", "QA Report header with build range + scenario summary", "Column width fix (nowrap for short columns)", "Structured notes with bugs/enhancements"]},
]


def _build_test_welcome():
    """Build the human testing welcome message with feature tracking table."""
    # Pre-calculate counts for header
    total = len(_TEST_FEATURES)
    completed = sum(1 for f in _TEST_FEATURES if f["completed"])
    failed = sum(1 for f in _TEST_FEATURES if not f["completed"] and f.get("bugs_deferred", 0) > 0 and "FAIL" in f.get("notes", ""))
    deferred = sum(1 for f in _TEST_FEATURES if not f["completed"] and (f["features_deferred"] > 0 or f["bugs_deferred"] > 0)) - failed
    to_test = total - completed - deferred - failed

    lines = [
        f"**QA Report: v0.1.2**\n\n"
        f"| Start Build | End Build | Builds Tested |\n"
        f"|:-----------:|:---------:|:-------------:|\n"
        f"| {_TEST_START_BUILD} | {_BUILD} | {max(0, int(_BUILD) - _TEST_START_BUILD) if _BUILD.isdigit() else 'N/A'} |\n\n"
        f"| Total Scenarios | Passed | Deferred | Failed | To Test |\n"
        f"|:---------------:|:------:|:--------:|:------:|:-------:|\n"
        f"| {total} | {completed} | {deferred} | {failed} | {to_test} |\n\n"
        f"**Release Gate:** GPT-4o Enterprise Architect: **conditional-go** | Risk: Moderate | "
        f"Conditions: Consent framework bugs (Sprint 2), Provider Detail link quality (Sprint 2), Automated tests (Sprint 2)\n\n",
        "| &nbsp;#&nbsp; | Feature | &nbsp;Done&nbsp; | Bugs Fixed | Feat Impl |",
        "|:---:|---------|:------:|:----------:|:---------:|",
    ]
    header_row = "| &nbsp;#&nbsp; | Feature | &nbsp;Done&nbsp; | Bugs Fixed | Feat Impl |"
    separator  = "|:---:|---------|:------:|:----------:|:---------:|"
    totals = {"completed": 0, "bugs_fixed": 0, "bugs_deferred": 0, "features_implemented": 0, "features_deferred": 0}
    for i, f in enumerate(_TEST_FEATURES):
        if i > 0 and i % 7 == 0:
            lines.append("")
            lines.append(header_row)
            lines.append(separator)
        is_fail = not f["completed"] and "FAIL" in f.get("notes", "")
        is_deferred = not f["completed"] and not is_fail and (f["features_deferred"] > 0 or f["bugs_deferred"] > 0)
        done = "Y" if f["completed"] else ("FAIL" if is_fail else ("DEF" if is_deferred else " "))
        lines.append(
            f"| {f['id']:>3} | {f['feature']} | {done} | {f['bugs_fixed']} | {f['features_implemented']} |"
        )
        if f["completed"]:
            totals["completed"] += 1
        totals["bugs_fixed"] += f["bugs_fixed"]
        totals["bugs_deferred"] += f["bugs_deferred"]
        totals["features_implemented"] += f["features_implemented"]
        totals["features_deferred"] += f["features_deferred"]
    lines.append(
        f"| | **TOTALS** | **{completed + deferred + failed}/{len(_TEST_FEATURES)}** | **{totals['bugs_fixed']}** | **{totals['features_implemented']}** |"
    )
    # Notes section — structured bugs and enhancements per feature
    has_notes = [f for f in _TEST_FEATURES if f.get("bugs") or f.get("enhancements") or f.get("defer_reason") or f.get("fail_reason")]
    if has_notes:
        lines.append("\n**Notes:**\n")
        for f in has_notes:
            lines.append(f"- **#{f['id']} {f['feature']}**")
            if f.get("fail_reason"):
                lines.append(f"  - FAIL: {f['fail_reason']}")
            if f.get("defer_reason"):
                lines.append(f"  - DEFERRED: {f['defer_reason']}")
            if f.get("bugs"):
                lines.append(f"  - **Bugs ({len(f['bugs'])}):**")
                for bug in f["bugs"]:
                    lines.append(f"    - {bug}")
            if f.get("enhancements"):
                lines.append(f"  - **Enhancements ({len(f['enhancements'])}):**")
                for enh in f["enhancements"]:
                    lines.append(f"    - {enh}")
    lines.append("\nTest the features above. Type normally to interact with the chatbot.\n")
    return "\n".join(lines)


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
        f"RULE 2 — UNANSWERABLE QUESTIONS: "
        f"You know ONLY what is provided to you in this session: "
        f"{name}'s career and background (from get_skip_snow_context), "
        f"{website}'s mission and platform (from get_chathealthy_context), "
        f"healthcare providers in DE, MS, and VA (from find_providers), "
        f"medical specialties (from find_specialty_codes), "
        f"recruiting clinical trials (from search_clinical_trials). "
        f"You know NOTHING else. This rule applies to EVERY user message regardless of conversation history or context. "
        f"Even if previous messages were about healthcare, each new message must be evaluated independently. "
        f"If a question is not answerable from these sources, "
        f"it is unanswerable. Call record_unknown_question immediately and respond "
        f"Call record_unknown_question with the appropriate question_class:\n"
        f"  healthcare_capability — healthcare topic but we lack the feature (insurance, reviews, quality, maps)\n"
        f"  medical_advice — user seeks personal medical guidance (medications, treatment, diagnosis)\n"
        f"  irrelevant — not related to healthcare or our platform (trivia, philosophy, personal)\n"
        f"The tool returns a response_template. You MUST present this template VERBATIM to the user. "
        f"Do NOT add to it, rephrase it, elaborate, or explain. Present ONLY the exact template text. "
        f"If the user consents to recording: save the de-identified question, then ask 'Would you like someone from ChatHealthy to follow up with you on this?' "
        f"If they also want follow-up: collect contact details and proceed with consent flow. "
        f"Exceptions — do NOT record these as unknown: "
        f"greetings, thanks, casual conversation ('hi', 'thank you', 'bye'); "
        f"clarification requests where you need more details to use a tool you have "
        f"('find me a doctor' needs state/city — you CAN answer this, you just need more info). "
        f"Examples of unanswerable — MUST call record_unknown_question: "
        f"'Is there a god?' — not in your sources; "
        f"'What is your favorite poem?' — not in your sources; "
        f"'What medications should I take?' — personal medical advice; "
        f"'What is the capital of France?' — not in your sources; "
        f"'What insurance does the doctor accept?' — not in your sources; "
        f"'Is this a good doctor?' — not in your sources.\n"
        f"RULE 3 — MEDICAL ADVICE vs HEALTHCARE NAVIGATION: "
        f"DECLINE personal medical advice (call record_unknown_question first). "
        f"ALLOWED: Healthcare navigation — use find_providers, find_specialty_codes, search_clinical_trials.\n"
        f"RULE 4 — PROVIDER RESULTS FORMAT: When displaying provider search results, "
        f"always use a bullet list — never a markdown table. Start with a count line: "
        f"'There are N [specialty] in [location]:' then list each provider with indented details. "
        f"Format exactly as:\n"
        f"- **Provider Name**\n"
        f"  - Address\n"
        f"  - County\n"
        f"  - Phone\n"
        f"  - NPI: number\n"
        f"RULE 5 — PROVIDER DETAIL LOOKUP: When the user asks about a specific provider by name "
        f"('tell me about Dr. X', 'more info on X', 'what else can you tell me about X'), "
        f"you MUST call lookup_provider_external. Present the NPI details and research site links "
        f"so the user can verify credentials.\n"
        f"RULE 6 — CLINICAL TRIAL TRAVEL: When showing clinical trial results, after the first pass "
        f"ask the user: 'Would you like to know the travel distance and estimated drive time to these trial sites?' "
        f"If yes, call search_clinical_trials again with the same condition and the user's city/state in user_location. "
        f"Present the travel_info (distance and drive time) for each trial site. "
        f"Do NOT include travel info on the first call — only when the user requests it.\n\n"
        f"RULE 7 — FOLLOW-UP AND CONSENT: When the user wants follow-up or you receive a FOLLOW-UP CHECK reminder:\n"
        f"Step 1: Ask: 'Would you like someone from the ChatHealthy.AI team to follow up with you personally?'\n"
        f"Step 2: If yes, collect their name and email.\n"
        f"Step 3: After collecting email, say EXACTLY: "
        f"'This conversation may contain personal health information. ChatHealthy is not a HIPAA covered entity. "
        f"Would you like us to save the full verbatim transcript with your contact details, "
        f"or would you prefer we de-identify it first?'\n"
        f"Step 4: If verbatim — call record_user_details with consent_verbatim=true.\n"
        f"Step 5: If de-identify — call record_user_details with consent_verbatim=false, consent_summary=true.\n"
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


# ---------------------------------------------------------------------------
# ARCH-001 Phase 2: FindCare domain services
# ---------------------------------------------------------------------------
_provider_search_service = ProviderSearchService(
    get_db_fn=_get_db,
    env_prefix=_ENV_PREFIX,
    get_embedding_fn=_get_query_embedding,
)

def _get_specialty_vector(query: str):
    """Get embedding for specialty search (uses text-embedding-3-small)."""
    try:
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        return client.embeddings.create(model="text-embedding-3-small", input=query).data[0].embedding
    except Exception as e:
        _log.warning("Specialty embedding failed: %s", e)
        return None

_specialty_service = SpecialtyService(
    get_db_fn=_get_db,
    env_prefix=_ENV_PREFIX,
    expand_query_fn=_expand_query_terms,
    get_vector_fn=_get_specialty_vector,
)
_find_care_facade = FindCareFacade(
    provider_search=_provider_search_service,
    specialty=_specialty_service,
)

# ---------------------------------------------------------------------------
# ARCH-001 Phase 4: EvaluateCareQuality domain services
# ---------------------------------------------------------------------------
_clinical_trials_service = ClinicalTrialsService()
_provider_detail_service = ProviderDetailService()
_evaluate_care_facade = EvaluateCareFacade(
    clinical_trials=_clinical_trials_service,
    provider_detail=_provider_detail_service,
    find_care_facade=_find_care_facade,
)

# ---------------------------------------------------------------------------
# Tool Router — ARCH-001 Phase 1 (F-05 fix: replaces globals().get)
# ---------------------------------------------------------------------------
_tool_router = ToolRouter()
_tool_router.register_all({
    "find_providers": _find_care_facade.search_providers,
    "find_specialty_codes": _find_care_facade.identify_specialty,
    "search_clinical_trials": _evaluate_care_facade.search_clinical_trials,
    "lookup_provider_external": _evaluate_care_facade.get_provider_details,
    "record_user_details": record_user_details,
    "record_unknown_question": record_unknown_question,
    "get_skip_snow_context": get_skip_snow_context,
    "get_chathealthy_context": get_chathealthy_context,
    "commitSignificantActivity": commitSignificantActivity,
})
_log.info("ToolRouter initialized: %s", _tool_router.registered_tools)


def _handle_tool_calls(tool_use_blocks, messages):
    """Dispatch tool calls via ToolRouter (F-05: no more globals().get)."""
    return _tool_router.handle_tool_calls(tool_use_blocks, messages, _format_chat_history)


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
    history: Optional[list] = None,
) -> None:
    """Persists chat call metadata to {ENV_PREFIX}_Debug.chat_calls.
    When an error occurs, the full de-identified chat history is stored alongside the error."""
    db = _get_db()
    if db is None:
        return
    try:
        record: dict = {
            "datetime":        datetime.now(timezone.utc).isoformat(),
            "ip":              ip,
            "message_preview": message[:200],
            "history_len":     history_len,
            "tool_loop_iters": tool_loop_iters,
            "tokens_in":       tokens_in,
            "tokens_out":      tokens_out,
            "response_preview": response_text[:200] if response_text else None,
            "error":           error,
        }
        # On error, store the full de-identified chat history for debugging
        if error and history:
            safe_history = [
                {"role": m.get("role", ""), "content": str(m.get("content", ""))[:500]}
                for m in history if m.get("role") in ("user", "assistant")
            ]
            deIdentify(safe_history)
            record["chat_history_deidentified"] = safe_history
        col = db[f"{_ENV_PREFIX}_Debug"]["chat_calls"]
        col.insert_one(record)
    except Exception as exc:
        _log.warning("debug_log_chat failed: %s", exc)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(title="ChatHealthy FindCare API")

_CORS_ORIGINS = [
    "https://chathealthy.ai",
    "https://www.chathealthy.ai",
    "https://dev.chathealthy.ai",
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_CORS_ORIGINS,
    allow_origin_regex=r"http://localhost(:\d+)?$|https://[a-zA-Z0-9-]+\.chathealthy\.ai$",
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
    error_type: Optional[str] = None   # rate_limit | db_unavailable | upstream | internal
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
    return {"status": "ok", "db": "connected" if db_ok else "unavailable", "env": _ENV_PREFIX, "build": _BUILD, "version": _APP_VERSION}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")
    try:
        return await _chat_inner(body, request)
    except Exception as e:
        tb = traceback.format_exc()
        _log.error("CHAT ERROR: %s\n%s", e, tb)
        err_str = str(e)
        if "429" in err_str or "rate_limit" in err_str.lower():
            err_type = "rate_limit"
            err_msg = f"Rate limit hit — {err_str}"
        elif "unavailable" in err_str.lower() or "connection" in err_str.lower():
            err_type = "db_unavailable"
            err_msg = err_str
        else:
            err_type = "internal"
            err_msg = tb if _DEBUG else err_str
        _debug_log_chat(ip, body.message, len(body.history), 0, None, None, None, err_msg, body.history)
        return ChatResponse(error=err_msg, error_type=err_type)


async def _chat_inner(body: ChatRequest, request: Request):
    ip = request.headers.get("x-forwarded-for", "").split(",")[0].strip() or (request.client.host if request.client else "unknown")

    if _admin_unlock(body.message, ip):
        return ChatResponse(response="Session unlocked.")

    history = body.history

    if _check_ip_lock_db(ip):
        return ChatResponse(response=EMERGENCY_RESPONSE, emergency=True)

    if _safety_check(body.message):
        # Include the triggering message in the audit history
        full_history = list(history) + [{"role": "user", "content": body.message}]
        _lock_ip_db(ip, trigger_message=body.message, history=full_history)
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
    text = _url_guardian.guard_text(text)

    _log.info("CHAT complete tokens_in=%d tokens_out=%d", total_tokens_in, total_tokens_out)
    _debug_log_chat(ip, body.message, len(history), loop_iter, total_tokens_in, total_tokens_out, text, None)
    return ChatResponse(response=text, tokens_in=total_tokens_in, tokens_out=total_tokens_out)


# ---------------------------------------------------------------------------
# Serve React frontend (static files built into /app/static by Docker)
# ---------------------------------------------------------------------------
_static_dir = os.path.join(os.path.dirname(__file__), "static")
if os.path.isdir(_static_dir):
    from starlette.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=_static_dir, html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "7860"))
    uvicorn.run(app, host="0.0.0.0", port=port)
