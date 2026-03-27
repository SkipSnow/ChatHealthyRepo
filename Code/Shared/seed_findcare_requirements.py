"""
Seed FindCare product requirements into Machine Brain.

Derived by reverse-engineering Code/ConversationalUX/ChatHealthyWhoAmIChat/app.py.
Run once per environment: ENV_PREFIX=dev python seed_findcare_requirements.py

These records are the source of truth for GPT planning assignments.
They are fetched by brain_runner.py and injected into every GPT prompt.
"""

import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# Bridge env var for Machine Brain
if not os.getenv("MONGODB_CONNECTION_STRING"):
    fc = os.getenv("MONGO_FRONTEND_connectionString")
    if fc:
        os.environ["MONGODB_CONNECTION_STRING"] = fc

sys.path.insert(0, str(Path(__file__).parent))
from machine_brain import store_decision

REQUIREMENTS = [

    # ── Safety Filter ──────────────────────────────────────────────────────────

    {
        "adr_id": "REQ-SAFETY-001",
        "topic": "Safety Filter — Dual Detection Architecture",
        "decision": (
            "Every user message is screened before reaching the LLM. "
            "Detection is dual: (1) keyword list for fast pattern matching, "
            "(2) Claude Haiku semantic classifier for ambiguous cases. "
            "If EITHER triggers, the session is hard-stopped and the user is directed to 911 or ER."
        ),
        "rationale": (
            "Keyword list catches obvious emergencies instantly with zero latency. "
            "Haiku catches semantic variants ('I want to end it all') that keywords miss. "
            "Dual detection with OR logic maximises recall — false negatives are unacceptable."
        ),
        "constraints": [
            "Safety check runs BEFORE any LLM call — no exceptions",
            "Haiku confidence threshold is 0.80 — below this, classify as PROBE not EMERGENCY",
            "If Haiku API call fails, default to EMERGENCY (fail-safe)",
            "Emergency response text is fixed — no variation allowed",
            "IP is locked for 1 hour after any trigger — lock persists across sessions",
        ],
        "components": ["SafetyFilter", "ChatAgent", "ConversationalUX"],
        "type": "requirement",
        "created_by": "Claude",
    },

    {
        "adr_id": "REQ-SAFETY-002",
        "topic": "Safety Filter — Emergency Incident Logging",
        "decision": (
            "All emergency triggers are logged to MongoDB Safety.emergency_incidents collection. "
            "Each record contains: IP address, lock timestamp, expiry timestamp, "
            "trigger message (truncated to 500 chars), de-identified chat history (role/content, 300 char limit per message)."
        ),
        "rationale": (
            "Incident logging is required for compliance and pattern analysis. "
            "De-identification protects user privacy while preserving the safety signal."
        ),
        "constraints": [
            "No PII beyond IP address in the incident record",
            "Chat history is truncated and de-identified before logging",
            "If DB write fails, incident is memory-only — log the failure but do not crash",
            "IP lock remains in effect even if DB write fails",
        ],
        "components": ["SafetyFilter", "MongoDB", "Safety.emergency_incidents"],
        "type": "requirement",
        "created_by": "Claude",
    },

    {
        "adr_id": "REQ-SAFETY-003",
        "topic": "Safety Filter — Keyword List",
        "decision": (
            "Hard-coded keyword list for immediate pattern matching. "
            "Current keywords: chest pain, chest tightness, heart attack, can't breathe, "
            "cannot breathe, difficulty breathing, stroke, face drooping, severe bleeding, "
            "unconscious, passed out, overdose, suicide, suicidal, kill myself, seizure, "
            "severe allergic reaction, anaphylaxis, choking."
        ),
        "rationale": (
            "Keyword matching is O(n) and sub-millisecond. Catches the highest-risk phrases "
            "without any API call. List is conservative — includes clinical and colloquial variants."
        ),
        "constraints": [
            "Keyword list is hardcoded — changes require Boss approval and code deploy",
            "Case-insensitive matching",
            "Partial match is sufficient — 'chest pain' matches 'severe chest pain'",
        ],
        "components": ["SafetyFilter", "EMERGENCY_KEYWORDS"],
        "type": "requirement",
        "created_by": "Claude",
    },

    # ── Provider Search ────────────────────────────────────────────────────────

    {
        "adr_id": "REQ-PROVIDER-001",
        "topic": "Provider Search — Core User Story",
        "decision": (
            "User describes what they need in natural language (e.g. 'I need a cardiologist in Wilmington DE'). "
            "System maps the description to NPI taxonomy codes, queries providers_staging in MongoDB, "
            "and returns up to 5 matching providers with name, NPI, address, and taxonomy code. "
            "Current supported states: Delaware (DE) and Mississippi (MS) only."
        ),
        "rationale": (
            "This is the core FindCare product value proposition. "
            "Natural language input removes the barrier of knowing medical specialty names. "
            "NPI taxonomy mapping is the bridge between consumer language and provider database."
        ),
        "constraints": [
            "Only DE and MS are supported — all other states return a friendly not-supported message",
            "Maximum 10 providers returned per query",
            "State filter is required — always ask if not provided",
            "City filter is optional — regex match if provided",
            "Query PublicHealthData.providers_staging collection via App 2 (read only)",
        ],
        "components": ["find_providers", "find_specialty_codes", "MongoDB", "PublicHealthData"],
        "type": "requirement",
        "created_by": "Claude",
    },

    {
        "adr_id": "REQ-PROVIDER-002",
        "topic": "Provider Search — Specialty Code Mapping",
        "decision": (
            "find_specialty_codes(query) maps user language to NPI taxonomy codes using two strategies: "
            "(1) vector search on specialty embeddings (primary), "
            "(2) regex search on taxonomy descriptions (fallback). "
            "GPT-4o-mini expands the query terms before searching to improve recall."
        ),
        "rationale": (
            "Vector search finds semantically similar specialties ('heart doctor' → cardiology). "
            "Regex fallback ensures a result even when embeddings are unavailable. "
            "Query expansion handles colloquial terms and abbreviations."
        ),
        "constraints": [
            "Filters to individual provider groupings only — excludes facilities and labs",
            "Returns taxonomy code, classification, and specialization for each match",
            "Falls back to regex if vector index unavailable",
        ],
        "components": ["find_specialty_codes", "MongoDB", "PublicHealthData"],
        "type": "requirement",
        "created_by": "Claude",
    },

    {
        "adr_id": "REQ-PROVIDER-003",
        "topic": "Provider Search — Provider Record Schema",
        "decision": (
            "Provider results returned to user include: "
            "name (formatted from prefix/first/middle/last/suffix/credential), "
            "NPI number, primary taxonomy code, full practice address (line1, city, state, zip). "
            "Organisation providers use legal business name instead of individual name fields."
        ),
        "rationale": (
            "NPI is the universal provider identifier — required for any downstream lookup. "
            "Full address is required for Google Maps integration. "
            "Taxonomy code enables specialty verification."
        ),
        "constraints": [
            "Address must include all four components for Maps geocoding: line1, city, state, zip",
            "NPI must always be included — it is the unique key",
            "Entity type 1 = individual provider, entity type 2 = organisation",
        ],
        "components": ["find_providers", "ProviderCard", "GoogleMaps"],
        "type": "requirement",
        "created_by": "Claude",
    },

    # ── Google Maps ────────────────────────────────────────────────────────────

    {
        "adr_id": "REQ-MAPS-001",
        "topic": "Google Maps — Provider Location Display",
        "decision": (
            "Provider search results must display on a Google Map embedded in the chat message stream. "
            "Each provider pin shows name and address on click. "
            "Map renders inline as a rich component — not a link, not a new tab."
        ),
        "rationale": (
            "Seeing providers on a map is the primary UX decision driver for FindCare. "
            "Distance and location context is essential for healthcare navigation. "
            "Inline rendering keeps the conversation flow intact — matches ChatGPT/Claude.ai UX model."
        ),
        "constraints": [
            "Google Maps JavaScript API key required — stored as environment secret",
            "Geocoding uses provider address string — no lat/lng in the provider record today",
            "Map component renders only when find_providers returns results",
            "NEVER render map for unsupported states",
        ],
        "components": ["ProviderMapComponent", "GoogleMapsAPI", "Angular"],
        "type": "requirement",
        "created_by": "Claude",
    },

    # ── Chat Interface ─────────────────────────────────────────────────────────

    {
        "adr_id": "REQ-CHAT-001",
        "topic": "Chat Interface — Intent Routing",
        "decision": (
            "Single chat session handles all intents: FindCare provider search, "
            "clinical trials search, About ChatHealthy, and contact capture. "
            "No separate apps, no visible routing. The LLM routes silently via tool calls."
        ),
        "rationale": (
            "One session = one context. User should never need to switch apps or restart. "
            "Intent routing is the LLM's job — the user just talks."
        ),
        "constraints": [
            "Medical advice requests must be declined — refer to qualified professional",
            "Healthcare navigation (find doctor, find trials) is explicitly allowed",
            "About ChatHealthy questions answered from provided documents only — no hallucination",
            "Follow-up offer injected every 5 user messages if no contact collected",
        ],
        "components": ["ChatAgent", "FastAPI", "Angular"],
        "type": "requirement",
        "created_by": "Claude",
    },

    {
        "adr_id": "REQ-CHAT-002",
        "topic": "Chat Interface — HIPAA Consent Flow",
        "decision": (
            "Two-tier consent before storing contact details: "
            "Tier 1: 'May we save a verbatim transcript of this conversation?' "
            "Tier 2 (if Tier 1 declined): 'May we save a de-identified summary?' "
            "record_user_details is called ONCE per contact with both consent flags."
        ),
        "rationale": (
            "HIPAA compliance requires explicit consent for storing conversation content. "
            "Two-tier gives users meaningful choice between full transcript and anonymised summary."
        ),
        "constraints": [
            "Consent verbatim = true → store full chat_history",
            "Consent verbatim = false, summary = true → LLM summarise → deIdentify → store in notes",
            "Both false → contact fields only, no history",
            "record_user_details called exactly once per conversation — never twice",
        ],
        "components": ["record_user_details", "deIdentify", "MongoDB", "AboutUs.lead"],
        "type": "requirement",
        "created_by": "Claude",
    },

    # ── Angular + FastAPI Port ────────────────────────────────────────────────

    {
        "adr_id": "REQ-STACK-001",
        "topic": "Frontend Stack — Angular + FastAPI",
        "decision": (
            "Replace Gradio with Angular (frontend) + FastAPI (Python backend). "
            "Angular on Cloudflare Pages. FastAPI on Azure App Service B1. "
            "Angular calls FastAPI via REST. FastAPI contains all business logic currently in app.py."
        ),
        "rationale": (
            "Gradio is a prototyping tool — it cannot support rich inline components like maps, "
            "provider cards, or interactive elements. Angular provides the component model needed. "
            "FastAPI is production-grade, async, and matches the existing Python skill set."
        ),
        "constraints": [
            "Angular frontend → Cloudflare Pages (free tier, per CFO directive)",
            "FastAPI backend → Azure App Service B1 (api-dev.chathealthy.ai)",
            "All business logic stays in Python (FastAPI) — Angular is UI only",
            "Chat endpoint: POST /chat with message + history",
            "Provider endpoint: available as a tool call from within the chat flow",
            "MONGO_connectionString must be set as Azure App Service environment variable",
        ],
        "components": ["Angular", "FastAPI", "CloudflarePages", "AzureAppService"],
        "type": "requirement",
        "created_by": "Claude",
    },

    {
        "adr_id": "REQ-STACK-002",
        "topic": "MongoDB Connection — Environment Secret",
        "decision": (
            "MONGO_connectionString must be set as an environment variable in every deployment target. "
            "Current blocker: HuggingFace Space does not have this secret set — provider search silently fails. "
            "Fix: add MONGO_connectionString to HuggingFace Space secrets. "
            "For FastAPI/Azure: set as Azure App Service application setting."
        ),
        "rationale": (
            "MongoDB connection uses lazy init — app starts even without the secret, "
            "but all DB-dependent features (provider search, contact recording, safety logging) silently fail. "
            "This was the root cause of the HuggingFace provider search failure."
        ),
        "constraints": [
            "Connection string must NEVER appear in code or committed files",
            "Must be set in: HuggingFace Space secrets, Azure App Service settings, local .env",
            "App must start gracefully without it — DB features degrade, core chat continues",
        ],
        "components": ["MongoDB", "ChatHealthyMongoUtilities", "HuggingFace", "AzureAppService"],
        "type": "requirement",
        "created_by": "Claude",
    },
]


def seed():
    print(f"Seeding {len(REQUIREMENTS)} FindCare product requirements into Machine Brain...")
    ok = 0
    for r in REQUIREMENTS:
        try:
            result = store_decision(
                topic=r["topic"],
                decision=r["decision"],
                rationale=r["rationale"],
                constraints=r.get("constraints", []),
                components=r.get("components", []),
                adr_id=r["adr_id"],
                risk="Low",

                created_by=r.get("created_by", "Claude"),
                decision_type=r.get("type", "requirement"),
            )
            status = "OK" if result else "FAIL"
            print(f"  [{status}] {r['adr_id']} — {r['topic']}")
            if result:
                ok += 1
        except Exception as e:
            print(f"  [ERR] {r['adr_id']} — {e}")

    print(f"\nDone: {ok}/{len(REQUIREMENTS)} requirements stored.")


if __name__ == "__main__":
    seed()
