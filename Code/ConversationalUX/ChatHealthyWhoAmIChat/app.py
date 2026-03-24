# Copyright © 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Coded by Claude Sonnet 4.6 (Anthropic).
# Developed in collaboration with ChatGPT (OpenAI).

from dotenv import load_dotenv
from openai import OpenAI
from anthropic import Anthropic
from concurrent.futures import ThreadPoolExecutor
import json
import os
import requests
import time
from datetime import datetime, timedelta, timezone
from pypdf import PdfReader
import gradio as gr
from ChatHealthyMongoUtilities import ChatHealthyMongoUtilities


load_dotenv(override=True)

SUPPORTED_STATES = {"DE", "MS"}

# ──────────────────────────────────────────────────────────────────────────────
# SAFETY DIRECTIVE — Authority: Skip the Boss — 2026-03-24
# Hard system invariant. Do not modify without explicit approval.
# ──────────────────────────────────────────────────────────────────────────────

# Rule 5: Fixed response — no variation allowed
EMERGENCY_RESPONSE = (
    "<b>Call 911 or go to the nearest emergency room immediately. Do not wait.</b>\n\n"
    "<b>This chat has been suspended.</b>"
)

# Rule 3: Rule-based signal list
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


# IP-based emergency lock — persists across Gradio sessions for 1 hour
_ip_emergency_locks: dict[str, float] = {}  # ip -> expiry (time.time() + 3600)
_EMERGENCY_LOCK_SECONDS = 3600

# Backdoor: set ADMIN_UNLOCK_KEY env var; send exactly "UNLOCK:<key>" to release an IP
_ADMIN_UNLOCK_KEY = os.getenv("ADMIN_UNLOCK_KEY", "")


def _is_ip_locked(ip: str) -> bool:
    """Fast in-memory check. Used as cache; DB is the source of truth."""
    expiry = _ip_emergency_locks.get(ip)
    if expiry is None:
        return False
    if time.time() < expiry:
        return True
    del _ip_emergency_locks[ip]
    return False


def _lock_ip(ip: str) -> None:
    """Set in-memory lock (cache)."""
    _ip_emergency_locks[ip] = time.time() + _EMERGENCY_LOCK_SECONDS


def _safety_collection():
    """Returns Safety.emergency_incidents collection or None."""
    db = _get_db()
    if db is None:
        return None
    try:
        return db["Safety"]["emergency_incidents"]
    except Exception:
        return None


def _lock_ip_db(ip: str, trigger_message: str = "", history: list = None) -> None:
    """Insert a new emergency incident and reset the 1-hour clock.

    Every attempt by a locked IP creates a new incident record and extends
    the expiry. Stores de-identified chat history (role/content only, truncated).
    """
    _lock_ip(ip)
    col = _safety_collection()
    if col is None:
        print("SAFETY DB unavailable — lock stored in memory only.", flush=True)
        return
    now = datetime.now(timezone.utc)
    expires = now + timedelta(seconds=_EMERGENCY_LOCK_SECONDS)
    safe_history = []
    for m in (history or []):
        if m.get("role") in ("user", "assistant"):
            safe_history.append({
                "role": m["role"],
                "content": str(m.get("content", ""))[:300],
            })
    try:
        col.insert_one({
            "ip": ip,
            "locked_at": now.isoformat(),
            "expires_at": expires.isoformat(),
            "trigger_message": trigger_message[:500],
            "chat_history_deidentified": safe_history,
        })
        print(f"SAFETY incident created: {ip} locked until {expires.isoformat()}", flush=True)
    except Exception as e:
        print(f"SAFETY DB write failed: {e}", flush=True)


def _check_ip_lock_db(ip: str) -> bool:
    """Check MongoDB for any active emergency incident for this IP.

    Uses in-memory cache as fast path; falls back to DB for cross-worker
    and post-restart cases. Returns True if any non-expired incident exists.
    """
    if _is_ip_locked(ip):
        return True
    col = _safety_collection()
    if col is None:
        return False
    try:
        now_iso = datetime.now(timezone.utc).isoformat()
        record = col.find_one({"ip": ip, "expires_at": {"$gt": now_iso}})
        if record:
            _lock_ip(ip)  # restore cache
            return True
        return False
    except Exception as e:
        print(f"SAFETY DB check failed: {e} — treating as unlocked", flush=True)
        return False


def _admin_unlock(message: str, ip: str) -> bool:
    """Returns True if message is a valid admin unlock command and clears all locks."""
    if not _ADMIN_UNLOCK_KEY:
        return False
    if message.strip().upper() == f"UNLOCK:{_ADMIN_UNLOCK_KEY.upper()}":
        was_locked = ip in _ip_emergency_locks
        _ip_emergency_locks.pop(ip, None)
        col = _safety_collection()
        if col is not None:
            try:
                result = col.delete_many({"ip": ip})
                print(f"SAFETY admin unlock: {ip} — deleted {result.deleted_count} incident(s)", flush=True)
            except Exception:
                pass
        print(f"SAFETY admin unlock: {ip} (was_locked={was_locked})", flush=True)
        return True
    return False


def _session_is_locked(history: list) -> bool:
    """Returns True if this session already contains the emergency response.

    Fallback lock that works even if the IP dict is lost (container restart,
    multi-worker environment). If the emergency response is in the history,
    the session is permanently locked regardless of IP state.
    """
    return any(
        m.get("role") == "assistant" and EMERGENCY_RESPONSE in str(m.get("content", ""))
        for m in history
    )


def _safety_check(message: str) -> bool:
    """Dual detection: keyword rules + Haiku semantic check.

    Returns True if emergency escalation is required.
    If EITHER detector triggers → True.
    If any failure → True (Rule 6: failure default is escalation).
    """
    msg_lower = message.lower()

    # Rule 3a: keyword detection
    keyword_hit = any(kw in msg_lower for kw in EMERGENCY_KEYWORDS)

    # Rule 3b: AI semantic detection (Haiku)
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
                "PROBE — these are NOT emergencies, they are probing questions:\n"
                "  'I'm in pain' — location unknown, severity unknown → PROBE\n"
                "  'I'm in a lot of pain' — location unknown → PROBE\n"
                "  'I don't feel well' → PROBE\n"
                "  'something hurts' → PROBE\n"
                "  'I feel sick' → PROBE\n"
                "  ANY pain or discomfort statement without a specific body location "
                "and stated severity → PROBE\n\n"
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
        # Require high confidence (>=0.80) for AI escalation — prevents vague pain
        # statements ("I'm in a lot of pain") from triggering the hard stop.
        # Approved reduction for ambiguous cases by Skip the Boss 2026-03-24.
        ai_hit = bool(result.get("emergency", False)) and confidence >= 0.80
        print(
            f"SAFETY keyword={keyword_hit} ai={ai_hit} "
            f"confidence={confidence:.2f} "
            f"triggered={keyword_hit or ai_hit}",
            flush=True,
        )
    except Exception as exc:
        # Rule 6: failure default -> escalate
        print(f"SAFETY check failed ({exc}) -> defaulting to escalation", flush=True)
        return True

    return keyword_hit or ai_hit

# MongoDB - lazy connection (connects on first use; app starts even if Mongo is unreachable)
_mongo_conn_str = os.getenv("MONGO_connectionString") or ""
DBManager = ChatHealthyMongoUtilities(
    _mongo_conn_str,
    connect_timeout_ms=10000,
    server_selection_timeout_ms=15000,
    max_retries=2,
) if _mongo_conn_str else None


def _get_db():
    """Returns MongoDB database or None if connection fails or missing. Logs and continues on failure."""
    if DBManager is None:
        return None
    try:
        return DBManager.getConnection()
    except Exception as e:
        print(f"MongoDB unavailable (chat will work; recordings skipped): {e}", flush=True)
        return None


# SparkPost email notifications
_SPARKMAIL_API_KEY = os.getenv("SPARKMAIL_API_KEY")
_SPARKMAIL_FROM    = os.getenv("NOTIFICATION_FROM_EMAIL")
_SPARKMAIL_TO      = os.getenv("NOTIFICATION_TO_EMAIL")


def push(message):
    print(f"Push: {message}")
    if not (_SPARKMAIL_API_KEY and _SPARKMAIL_FROM and _SPARKMAIL_TO):
        print("SparkPost credentials not configured — notification suppressed.")
        return
    try:
        from sparkpost import SparkPost
        sp = SparkPost(_SPARKMAIL_API_KEY)
        sp.transmissions.send(
            recipients=[_SPARKMAIL_TO],
            from_email=_SPARKMAIL_FROM,
            subject="ChatHealthy — Activity",
            text=message,
        )
    except Exception as exc:
        print(f"SparkPost send failed: {exc}")


def commitSignificantActivity(payload=None, **kwargs):
    """Accept a JSON payload and insert into DB. Payload: {database, collection, record}."""
    from datetime import datetime
    db = _get_db()
    if db is None:
        return {"recorded": "ok", "note": "MongoDB unavailable; record not persisted"}
    payload = payload or kwargs
    if isinstance(payload, str):
        payload = json.loads(payload)
    database = payload["database"]
    collection = payload["collection"]
    record = dict(payload["record"])
    record["record_number"] = db[database][collection].count_documents({}) + 1
    record["datetime"] = datetime.now().isoformat()
    db[database][collection].insert_one(record)
    return {"recorded": "ok"}


def _format_chat_history(messages):
    """Extract user/assistant content from messages for storage."""
    out = []
    for m in messages:
        if m.get("role") not in ("user", "assistant"):
            continue
        c = m.get("content")
        text = str(c)[:500] if c else ""
        out.append({"role": m["role"], "content": text})
    return out


def record_user_details(email="", name="Name not provided", notes="not provided", message="",
                        chat_history=None, consent_verbatim=False, consent_summary=None, testdata=False):
    if not email or not str(email).strip():
        return {"recorded": "ok", "note": "Email required but not provided"}
    db = _get_db()
    if db is None:
        push(f"Recording interest from {name} with email {email} (DB unavailable)")
        return {"recorded": "ok", "note": "MongoDB unavailable; contact logged via push only"}
    reason = message or notes
    lead_coll = db["Users"]["users"]
    for doc in lead_coll.find():
        if email in str(doc.get("email", "")):
            return {"recorded": "ok"}
    push(f"Recording interest from {name} with email {email}: {reason}")
    record = {
        "email": email,
        "name": name,
        "notes": notes,
        "reason_for_contact": reason,
        "consent_verbatim": consent_verbatim,
        "consent_summary": consent_summary,
        "datetime": datetime.now().isoformat(),
        "testdata": testdata,
    }
    if consent_verbatim:
        record["chat_history"] = chat_history or []
    elif consent_summary:
        summary = _summarize_conversation(chat_history)
        summary_msg = [{"role": "user", "content": summary}]
        deIdentify(summary_msg)
        record["notes"] = summary_msg[0]["content"]
    payload = {"database": "Users", "collection": "users", "record": record}
    commitSignificantActivity(payload)
    return {"recorded": "ok"}


def _summarize_conversation(chat_history):
    """Ask Claude Haiku to produce a brief 2-3 sentence summary of the conversation."""
    if not chat_history:
        return ""
    client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    chat_json = json.dumps(
        [{"role": m.get("role", ""), "content": m.get("content") or ""} for m in chat_history],
        indent=2
    )
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=256,
        messages=[{"role": "user", "content": (
            "Summarize this conversation in 2-3 sentences. Focus on what the user wanted "
            "and any key information they shared. Be concise.\n\n" + chat_json
        )}],
    )
    return response.content[0].text.strip()


def deIdentify(argChat_history):
    """
    Takes the list used to create the history array in the output object, and deidentifies each member.
    Deidentification is sufficient for HIPAA Research Data. Uses Anthropic model claude-haiku-4-5-20251001.
    Batches the entire conversation in a single API call. Mutates argChat_history in place.
    """
    if not argChat_history:
        return
    client = Anthropic(api_key=os.getenv("Anthropic_API_KEY"))
    model = "claude-haiku-4-5-20251001"
    chat_json = json.dumps([{"role": m.get("role", ""), "content": m.get("content") or ""} for m in argChat_history], indent=2)
    deidentify_prompt = """Deidentify the following chat conversation so it meets HIPAA Safe Harbor requirements for research data.
Remove or replace: names, geographic identifiers (except state), dates (except year), phone/fax, email, SSN,
medical record numbers, account numbers, license numbers, vehicle identifiers, device identifiers, URLs, IP addresses,
and any other identifiers that could be used to identify an individual.
Preserve the semantic meaning of each message for research purposes.

Return ONLY a valid JSON array of strings, one string per message in the same order. Each string is the deidentified content for that message.
Example: ["deidentified msg 1", "deidentified msg 2"]

Chat conversation:
"""
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        messages=[{"role": "user", "content": f"{deidentify_prompt}\n{chat_json}"}],
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


def _expand_query_terms(query: str) -> list[str]:
    """Use Haiku to expand a specialty query into keyword stems for regex search."""
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
    """Hybrid search: vector search for matching classifications + regex search for
    keyword stems. Returns the complete set of matching NUCC codes."""
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

    # Two fully independent pipelines run in parallel:
    # Pipeline 1: Haiku expands stems → regex search
    # Pipeline 2: OpenAI embedding → vector search
    def regex_pipeline():
        stems = _expand_query_terms(query)
        regex_clauses = [
            {field: {"$regex": stem, "$options": "i"}}
            for stem in stems
            for field in ("Specialization", "Display Name")
        ]
        codes = list(db["PublicHealthData"]["SpecialtyMetaData"].find(
            {"$and": [{"$or": regex_clauses}, individual_filter]}, projection
        )) if regex_clauses else []
        return codes, stems

    def vector_pipeline():
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        query_vector = client.embeddings.create(model="text-embedding-3-small", input=query).data[0].embedding
        top = list(db["PublicHealthData"]["SpecialtyMetaData"].aggregate([
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
        codes = list(db["PublicHealthData"]["SpecialtyMetaData"].find(
            {"$and": [{"Classification": {"$in": classifications}}, individual_filter]}, projection
        )) if classifications else []
        return codes, classifications

    with ThreadPoolExecutor(max_workers=2) as ex:
        rf = ex.submit(regex_pipeline)
        vf = ex.submit(vector_pipeline)
        regex_codes, stems = rf.result()
        vector_codes, classifications = vf.result()

    # Step 4: union, deduplicate by Code
    seen = set()
    all_codes = []
    for doc in vector_codes + regex_codes:
        if doc["Code"] not in seen:
            seen.add(doc["Code"])
            all_codes.append(doc)

    # --- DEV CODE START ---
    if "debug" in query.lower():
        return {
            "debug": True,
            "query": query,
            "stems_used_for_regex": stems,
            "regex_fields_searched": ["Specialization", "Display Name"],
            "classifications_from_vector_search": classifications,
            "total_codes_found": len(all_codes),
        }
    # --- DEV CODE END ---

    return {"specialties": all_codes, "matched_classifications": classifications, "stems": stems}


def find_providers(specialty_query: str, state: str, city: str = "", limit: int = 5) -> dict:
    """Find providers in supported states (DE, MS) by specialty.

    Branch 1 — supported state: queries providers_staging by taxonomy code + state filter.
    Branch 2 — unsupported state: returns structured not-supported response.
    """
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
        return {
            "supported": True,
            "providers": [],
            "message": f"No matching specialty found for '{specialty_query}'.",
        }

    query_filter = {
        "practice_address.state": state_upper,
        "taxonomies.code": {"$in": codes},
    }
    if city:
        query_filter["practice_address.city"] = {"$regex": city.strip(), "$options": "i"}

    projection = {
        "_id": 0,
        "npi": 1,
        "entity_type_code": 1,
        "provider_first_name": 1,
        "provider_last_name_legal_name": 1,
        "provider_middle_name": 1,
        "provider_name_prefix_text": 1,
        "provider_name_suffix_text": 1,
        "provider_credential_text": 1,
        "provider_organization_name_legal_business_name": 1,
        "practice_address": 1,
        "taxonomies": 1,
    }

    raw = list(
        db["PublicHealthData"]["providers_staging"]
        .find(query_filter, projection)
        .limit(min(int(limit), 10))
    )

    if not raw:
        return {
            "supported": True,
            "providers": [],
            "message": f"No {specialty_query} providers found in {state_upper}.",
        }

    providers = []
    for p in raw:
        if p.get("entity_type_code") == "1":
            parts = [
                p.get("provider_name_prefix_text"),
                p.get("provider_first_name"),
                p.get("provider_middle_name"),
                p.get("provider_last_name_legal_name"),
                p.get("provider_name_suffix_text"),
            ]
            name = " ".join(x for x in parts if x)
            if p.get("provider_credential_text"):
                name += f", {p['provider_credential_text']}"
        else:
            name = p.get("provider_organization_name_legal_business_name") or "Unknown Organization"

        addr = p.get("practice_address", {})
        addr_parts = [addr.get("line1"), addr.get("city"), addr.get("state"), addr.get("zip")]
        address = ", ".join(x for x in addr_parts if x)

        primary = next((t for t in p.get("taxonomies", []) if t.get("primary")), None)
        taxonomy_code = primary.get("code", "") if primary else ""

        providers.append({
            "name": name,
            "npi": p.get("npi", ""),
            "taxonomy_code": taxonomy_code,
            "address": address,
        })

    return {
        "supported": True,
        "state": state_upper,
        "specialty_searched": specialty_query,
        "count": len(providers),
        "providers": providers,
    }


def search_clinical_trials(condition: str, location: str = "", max_results: int = 5) -> dict:
    """Search ClinicalTrials.gov directly and return recruiting trial cards."""
    params = {
        "query.cond": condition,
        "filter.overallStatus": "RECRUITING",
        "pageSize": min(int(max_results), 10),
        "format": "json",
    }
    if location:
        params["query.locn"] = location
    try:
        response = requests.get(
            "https://clinicaltrials.gov/api/v2/studies",
            params=params,
            timeout=15,
        )
        response.raise_for_status()
        studies = response.json().get("studies", [])
    except Exception as exc:
        return {"error": f"ClinicalTrials.gov search failed: {exc}"}

    if not studies:
        return {"trials": [], "message": "No recruiting trials found for this condition."}

    trials = []
    for study in studies:
        ps = study.get("protocolSection", {})
        id_mod = ps.get("identificationModule", {})
        status_mod = ps.get("statusModule", {})
        desc_mod = ps.get("descriptionModule", {})
        elig_mod = ps.get("eligibilityModule", {})
        contacts_mod = ps.get("contactsLocationsModule", {})
        design_mod = ps.get("designModule", {})

        nct_id = id_mod.get("nctId", "")
        raw_locs = contacts_mod.get("locations", [])
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


def record_unknown_question(question, chat_history=None):
    if chat_history is not None:
        deIdentify(chat_history)
    push(f"Recording a user question I could not answer: {question}")
    payload = {
        "database": "DeidentifiedSessions", "collection": "unknown_questions",
        "record": {"question": question, "chat_history": chat_history or []}
    }
    commitSignificantActivity(payload)
    return {"recorded": "ok"}


record_user_details_json = {
    "name": "record_user_details",
    "description": (
        "Record a user's contact details after obtaining email. "
        "Before calling this tool you MUST complete the two-tier consent flow: "
        "First ask: 'May we save a verbatim transcript of this conversation with your contact details?' "
        "If they decline, ask: 'May we save a de-identified summary of this conversation instead?' "
        "Pass both answers as consent_verbatim and consent_summary. "
        "Try to get their name and reason for contact, but do not insist."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "email": {"type": "string", "description": "The email address of this user"},
            "name": {"type": "string", "description": "The user's name, if they provided it"},
            "notes": {"type": "string", "description": "Summarize the conversation in less than 40 words"},
            "message": {"type": "string", "description": "Why they are contacting us"},
            "consent_verbatim": {"type": "boolean", "description": "True if user agreed to save verbatim transcript"},
            "consent_summary": {"type": "boolean", "description": "True if user agreed to save de-identified summary. Only set when consent_verbatim is false. Null if verbatim was accepted."}
        },
        "required": ["email", "notes", "consent_verbatim"],
        "additionalProperties": False
    }
}

find_providers_json = {
    "name": "find_providers",
    "description": (
        "Search for healthcare providers (doctors, specialists) in a specific US state. "
        "FindCare currently covers Delaware (DE) and Mississippi (MS). "
        "Call this when the user asks to find a doctor, specialist, or provider in a location. "
        "Always confirm the user's state before calling if not provided. "
        "If result contains supported=false, tell the user we're not in their state yet, "
        "ask if they'd like to be notified when we expand, then call record_unknown_question "
        "with their state and query so we can follow up."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "specialty_query": {
                "type": "string",
                "description": "The type of doctor or specialty (e.g. 'cardiologist', 'pediatrician', 'dermatologist')"
            },
            "state": {
                "type": "string",
                "description": "Two-letter US state abbreviation (e.g. 'DE', 'MS', 'TX')"
            },
            "city": {
                "type": "string",
                "description": "Optional city name to narrow results"
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of providers to return (default 5, max 10)"
            },
        },
        "required": ["specialty_query", "state"],
        "additionalProperties": False,
    },
}

record_unknown_question_json = {
    "name": "record_unknown_question",
    "description": (
        "Call this tool BEFORE composing your response whenever ANY of the following is true: "
        "(1) The answer is not explicitly stated in the provided Summary, LinkedIn, or Anthropic documents. "
        "NOTE: Do NOT call this for FindCare queries (provider searches, specialty lookups, clinical trials, "
        "or unsupported-state notifications) — those have their own tools. "
        "(2) You would use any hedging word such as 'I think', 'probably', 'might', 'I believe', 'I'm not sure', "
        "'it seems', 'I'd imagine', 'I'd guess', or any similar qualifier. "
        "(3) The question is personal medical advice: diagnosis, treatment recommendations, medication guidance, "
        "'should I take X', 'is my symptom serious', interpreting test results. "
        "Do NOT apply this to healthcare navigation questions such as 'what does a [specialty] do?', "
        "'find me a [provider type]', or 'what kind of doctor treats X?' — those are allowed and use find_specialty_codes. "
        "(4) You are inferring or extrapolating rather than directly quoting the provided documents. "
        "Do NOT answer first and record second. The correct order is: call this tool, then tell the user you don't have that information."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "The exact question or topic that could not be answered with certainty from the provided documents"}
        },
        "required": ["question"],
        "additionalProperties": False
    }
}

commitSignificantActivity_json = {
    "name": "commitSignificantActivity",
    "description": "Record any custom activity to the database. Supply database, collection, and record (any JSON structure). Use for custom records beyond contacts and unknown questions.",
    "parameters": {
        "type": "object",
        "properties": {
            "database": {"type": "string", "description": "Database name"},
            "collection": {"type": "string", "description": "Collection name"},
            "record": {"type": "object", "description": "The document to insert - any JSON object. record_number and datetime are added automatically."}
        },
        "required": ["database", "collection", "record"]
    }
}

find_specialty_codes_json = {
    "name": "find_specialty_codes",
    "description": (
        "Look up NUCC provider taxonomy codes matching a medical specialty or provider type. "
        "Call this when the user asks about a type of doctor, specialist, or medical provider — "
        "for example 'cardiologist', 'OB-GYN', 'pediatrician', 'heart doctor'. "
        "Also call this when the user asks what a specific provider type does — "
        "for example 'what does a respiratory therapist do?', 'what is a neonatologist?'. "
        "Use the returned classification and specialization fields to explain the role. "
        "Returns matching taxonomy codes with classification and specialization details. "
        "If the result contains 'debug': true, display the full JSON result verbatim to the user — do not paraphrase it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The specialty or provider type to look up (e.g. 'cardiologist', 'pediatric surgeon')"}
        },
        "required": ["query"],
        "additionalProperties": False
    }
}

search_clinical_trials_json = {
    "name": "search_clinical_trials",
    "description": (
        "Search for actively recruiting clinical trials on ClinicalTrials.gov. "
        "Call this when the user asks to find trials, studies, or research programs for a medical condition. "
        "Returns trial cards with title, phase, locations, eligibility match, and a direct link to each trial. "
        "This is healthcare navigation — not medical advice."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "condition": {"type": "string", "description": "The medical condition or diagnosis to search for (e.g. 'stage 3 pancreatic cancer', 'type 2 diabetes')"},
            "location": {"type": "string", "description": "Optional city/state to filter by proximity (e.g. 'New York, NY')"},
            "max_results": {"type": "integer", "description": "Number of trials to return (default 5, max 10)"},
        },
        "required": ["condition"],
        "additionalProperties": False,
    },
}

tools = [
    {"type": "function", "function": find_providers_json},
    {"type": "function", "function": record_user_details_json},
    {"type": "function", "function": record_unknown_question_json},
    {"type": "function", "function": commitSignificantActivity_json},
    {"type": "function", "function": find_specialty_codes_json},
    {"type": "function", "function": search_clinical_trials_json},
]


# Path to me/ directory relative to this script (works for local runs and deployment)
_ME_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "me")


class Me:

    def __init__(self):
        self.openai = OpenAI()
        self.name = "Skip Snow"
        self.website = "ChatHealthy.AI"
        reader = PdfReader(os.path.join(_ME_DIR, "SkipSnowLinkedInProfile.pdf"))
        self.linkedin = ""
        for page in reader.pages:
            text = page.extract_text()
            if text:
                self.linkedin += text
        reader_anthropic = PdfReader(os.path.join(_ME_DIR, "BuildingAnthropicAConversationWithItsCo-foundersYouTube.pdf"))
        self.anthropic_discussion = ""
        for page in reader_anthropic.pages:
            text = page.extract_text()
            if text:
                self.anthropic_discussion += text
        with open(os.path.join(_ME_DIR, "summary.txt"), "r", encoding="utf-8") as f:
            self.summary = f.read()

    def handle_tool_calls(self, tool_calls, messages=None):
        chat_history = _format_chat_history(messages) if messages else []
        results = []
        for tool_call in tool_calls:
            tool_name = tool_call.function.name
            arguments = json.loads(tool_call.function.arguments)
            if tool_name in ("record_user_details", "record_unknown_question"):
                arguments["chat_history"] = chat_history
            print(f"Tool called: {tool_name}", flush=True)
            tool = globals().get(tool_name)
            result = tool(**arguments) if tool else {}
            results.append({"role": "tool", "content": json.dumps(result), "tool_call_id": tool_call.id})
        return results

    def system_prompt(self):
        return (
            f"You are acting as {self.name}. You are answering questions on {self.website}'s website, "
            f"particularly questions related to {self.name}'s career, background, skills and experience and plans the future of this web site. "
            f"Your responsibility is to represent {self.name} and {self.website} for interactions on the website as faithfully as possible. "
            f"You are given a summary of {self.name}'s background and LinkedIn profile which you can use to answer questions. "
            f"Be professional and engaging, as if talking to a potential client or future employer who came across the website. "
            f"\n\n## STRICT ANSWER RULES — NO EXCEPTIONS\n"
            f"RULE 0 — EMERGENCY: If the user describes ANY symptom or situation that could be a medical emergency "
            f"(chest pain, difficulty breathing, stroke symptoms, severe bleeding, loss of consciousness, "
            f"seizure, severe allergic reaction, overdose, suicidal thoughts, or any life-threatening situation), "
            f"STOP immediately. Do not use any tool. Respond ONLY with the exact text: "
            f"'{EMERGENCY_RESPONSE}' "
            f"Do not continue the conversation until the user confirms they are safe.\n"
            f"RULE 1 — SOURCE RESTRICTION: You may ONLY answer from facts explicitly stated in the Summary, LinkedIn, and Anthropic documents provided below. "
            f"You must NEVER use your general training knowledge to answer. If it is not in the documents, you do not know it.\n"
            f"RULE 2 — NO HEDGING: You are PROHIBITED from using any hedging language: 'I think', 'probably', 'might', "
            f"'I believe', 'I'm not sure', 'it seems', 'I'd imagine', 'I'd guess', or similar. "
            f"If you would reach for any of these words, that is your signal to call record_unknown_question instead of answering.\n"
            f"RULE 3 — MEDICAL ADVICE vs HEALTHCARE NAVIGATION: These are different things.\n"
            f"  DECLINE (call record_unknown_question first): Personal medical advice — diagnosis, treatment recommendations, "
            f"medication guidance, 'should I take X', 'is my symptom serious', interpreting test results. "
            f"Always refer the user to a qualified healthcare professional.\n"
            f"  ALLOWED — Healthcare navigation: This is the core purpose of ChatHealthy.ai. "
            f"Use find_providers when the user asks to find a doctor or specialist in a specific location — "
            f"always ask for their state if not provided. FindCare covers Delaware (DE) and Mississippi (MS). "
            f"If find_providers returns supported=false, tell the user we're not in their state yet, "
            f"ask if they'd like to be notified when we expand, then call record_unknown_question with their state and query. "
            f"Use find_specialty_codes when the user asks about types of doctors or specialists. "
            f"Use search_clinical_trials when the user asks to find clinical trials or research studies for a condition — "
            f"this is navigation, not advice. Present the results clearly with trial names, phases, locations, and links.\n"
            f"RULE 4 — TOOL CALL ORDER: Always call record_unknown_question BEFORE composing your response. Never answer first and record second.\n"
            f"RULE 5 — EACH QUESTION SEPARATELY: If a user asks multiple questions in one message and some are unknown, "
            f"record each unknown question with a separate tool call.\n"
            f"RULE 6 — FOLLOW-UP OFFER: When you receive a system FOLLOW-UP CHECK reminder, first review the conversation "
            f"for any sign of annoyance or reluctance at being asked about follow-up — such as 'stop asking', 'I already said no', "
            f"'leave me alone', 'not interested', or any impatient or irritated tone in response to a previous follow-up offer. "
            f"If any such signal exists, do NOT ask again — ignore the reminder entirely for the rest of the conversation. "
            f"Otherwise, assess whether the user has shown genuine interest in a specific topic — such as career inquiry, "
            f"investment, partnership, product interest, or healthcare navigation — AND you do not yet have their contact details. "
            f"If both are true, ask: 'Would you like someone from the ChatHealthy.AI team to follow up with you personally?' "
            f"If they say yes, proceed to collect their email and complete the consent flow. "
            f"If context is not yet sufficient (e.g. the user has only exchanged one or two brief messages), do not ask yet.\n"
            f"\n## EXAMPLES\n"
            f"User: What is your favorite poem?\n"
            f"WRONG: 'While I appreciate poetry, I don't have a specific favorite.' [no tool call — this is a violation]\n"
            f"RIGHT: [call record_unknown_question] then say: 'I don't have that information — I've noted your question and will follow up.'\n"
            f"User: Should I take ibuprofen for my back pain?\n"
            f"WRONG: 'While I'm not a doctor, ibuprofen can help with inflammation...' [medical advice — violation]\n"
            f"RIGHT: [call record_unknown_question] then say: 'I can't advise on medication or treatment. Please consult a qualified healthcare professional.'\n\n"
            f"User: I have stage 3 pancreatic cancer. Find me trials.\n"
            f"WRONG: [call record_unknown_question and decline] [navigation request — violation]\n"
            f"RIGHT: [call search_clinical_trials with condition='stage 3 pancreatic cancer'] then present the trial cards.\n\n"
            f"If the user is engaging in discussion, try to steer them towards getting in touch via email, phone or linkedin; ask for their email, or other method of communication. No authentication needed. "
            f"When you have their email, complete the two-tier consent flow before calling record_user_details:\n"
            f"  Step 1 — Ask: 'May we save a verbatim transcript of this conversation with your contact details?'\n"
            f"  Step 2 — If they decline Step 1, ask: 'May we save a de-identified summary of this conversation instead?'\n"
            f"  Then call record_user_details with consent_verbatim and (if asked) consent_summary reflecting their answers.\n"
            f"Call record_user_details only ONCE per contact. If you have already recorded this user's email in this conversation, do not call it again. "
            f"If the user gives an email and you don't know their name, capture their name too.\n\n"
            f"## Summary:\n{self.summary}\n\n## LinkedIn Profile:\n{self.linkedin}\n\n"
            f"## AnthropicOnSafety:\n{self.anthropic_discussion}\n\n"
            f"With this context, please chat with the user, always staying in character as {self.name}."
        )

    def chat(self, message, history, request: gr.Request = None):
        ip = (request.client.host if request and request.client else "unknown")

        # Backdoor — must check before the lock gate
        if _admin_unlock(message, ip):
            return "Session unlocked."

        # SAFETY GATE — Rule 4: hard stop, runs first, no exceptions.
        # Same-session: history already contains emergency response — just repeat, no new incident.
        if _session_is_locked(history):
            return EMERGENCY_RESPONSE
        # Cross-session lock (DB hit) or new trigger: create new incident, extend 1-hour clock.
        if _check_ip_lock_db(ip) or _safety_check(message):
            _lock_ip_db(ip, trigger_message=message, history=history)
            return EMERGENCY_RESPONSE

        messages = [{"role": "system", "content": self.system_prompt()}] + history
        user_msg_count = sum(1 for m in history if m.get("role") == "user")
        if user_msg_count > 0 and user_msg_count % 5 == 0:
            messages.append({
                "role": "system",
                "content": (
                    "FOLLOW-UP CHECK: Review the conversation. If the user has shown genuine interest "
                    "in a specific topic and you do not yet have their contact details, ask now: "
                    "'Would you like someone from the ChatHealthy.AI team to follow up with you personally?'"
                )
            })
        messages.append({"role": "user", "content": message})
        done = False
        while not done:
            response = self.openai.chat.completions.create(model="gpt-4o-mini", messages=messages, tools=tools)
            if response.choices[0].finish_reason == "tool_calls":
                msg = response.choices[0].message
                tool_calls = msg.tool_calls
                results = self.handle_tool_calls(tool_calls, messages)
                messages.append(msg)
                messages.extend(results)
            else:
                done = True
        return response.choices[0].message.content


if __name__ == "__main__":
    me = Me()
    welcome = (
        "**Welcome to ChatHealthy FindCare**\n\n"
        "Here's what I can help you with:\n\n"
        "- **Find a doctor** — search for providers in <span style=\"font-size:1.15em;font-weight:bold;\">Delaware</span> or <span style=\"font-size:1.15em;font-weight:bold;\">Mississippi</span> by specialty or condition\n"
        "- **Identify the right specialty** — not sure what kind of doctor you need? Describe your situation\n"
        "- **Clinical trials** — find recruiting research studies for any condition\n"
        "- **About ChatHealthy** — our mission, team, and platform\n\n"
        "If you think you may be having a medical emergency, tell me right away.\n\n"
        "**What can I help you with today?**"
    )
    gr.ChatInterface(
        me.chat,
        type="messages",
        title="",
        css="footer { display: none !important; }",
        chatbot=gr.Chatbot(
            value=[{"role": "assistant", "content": welcome}],
            type="messages",
        ),
    ).launch(share=True, show_api=False)
