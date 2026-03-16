# ChatHealthy FindCare — Project Context

## Diagrams
Always use simple boxes and lines with white backgrounds and black text.
All diagrams must be fully visible. Never cross lines. Never place objects on top of one another.

---

## Infrastructure

```
Cloudflare (www.chathealthy.ai)
  → Static marketing pages, DNS
  → Embeds HuggingFace space via iframe

HuggingFace Space
  → Layer 1: React + FastAPI (conversational interface)
  → Direct Anthropic SDK for chat
  → UX model: ChatGPT / Claude.ai — single session, rich components inline

Azure Function App (FindCare-AI, US East 2)
  → Layer 2: CrewAI workflows (long-running background tasks)
  → Python 3.12.10, pay-per-use
  → Core library: ChatHealthyLib.pipelines

MongoDB Atlas
  → Account: skip.snow@gmail.com
  → Dev cluster: ChatHealthyDB_dev
```

---

## GitHub

- **App repo**: `SkipSnow/findCare`
- **Pipeline repo**: `SkipSnow/ChatHealthyDataPipeline`
- CI/CD: push to master → GitHub Actions → deploys to HuggingFace
- HF_TOKEN stored as GitHub secret
- `tests/` and `.env` excluded from HuggingFace deploy

---

## Architecture Decisions

### UX: One unified session
- No separate About Us app — it is one intent path within the main session
- Intent classifier routes the conversation; the user never sees the routing
- Rich components (maps, charts, provider cards) render inline in the message stream
- Session transcript is a first-class output — users can share it with their doctor

### Layer 1 — Conversational interface (HuggingFace)
- React + shadcn/ui frontend
- FastAPI backend
- Direct Anthropic SDK — Claude handles routing, tool calls, and response
- Tools defined in FastAPI, called by Claude during conversation

### Layer 2 — Background workflows (Azure Functions)
- CrewAI multi-agent orchestration
- Multi-LLM: each agent uses the best model for its task (Claude, GPT-4o, others)
- Triggered by Layer 1 via API call when a complex workflow is needed
- Example workflows: clinical trial search, eligibility matching, document generation

### Why CrewAI over Anthropic Agent SDK
- CrewAI supports multiple LLMs per agent — needed for cost and capability optimization
- Anthropic Agent SDK is Claude-only

### FHIR / Epic EMR
- Deferred to a later phase
- Will plug into Layer 2

---

## Existing HuggingFace App (About Us Chat)

Still live at `SkipSnow/ChatHealthyWhoAmIChat`. Keep it running until FindCare replaces it.

**Key files:**
- `Code/HuggingBearCode/ChatHealthyWhoAmIChat/app.py` — main app
- `Code/HuggingBearCode/ChatHealthyWhoAmIChat/ChatHealthyMongoUtilities.py` — MongoDB connection
- `Code/HuggingBearCode/ChatHealthyWhoAmIChat/tests/` — unit + e2e tests

**Stack:** Python, Gradio 5.22, OpenAI gpt-4o-mini (chat), Claude Haiku (deIdentify + summarize)

---

## MongoDB Guidelines

- Use batch writes when possible
- Use connection pooling (design pending)
- Monitor and throttle — dev cluster uses shared CPU
- Tag all automated test records with `testdata: true`
- Never delete records by default — filter test data with `{ testdata: true }` when needed

### Collections
- `AboutUs.lead` — contact records (verbatim or de-identified, per consent)
- `AboutUs.AboutSkip` — unknown questions (de-identified)

### Lead Record Schema
```json
{
  "email": "...",
  "name": "...",
  "notes": "...",
  "reason_for_contact": "...",
  "consent_verbatim": true,
  "consent_summary": null,
  "chat_history": [...],
  "datetime": "ISO string",
  "testdata": false
}
```

---

## HIPAA Consent Flow

Two-tier consent before saving any chat data:

1. **Verbatim**: "May we save a verbatim transcript of this conversation with your contact details?"
   - Yes → store full `chat_history` (PII intact, consent exchange included as evidence)
2. **Summary** (if verbatim declined): "May we save a de-identified summary instead?"
   - Yes → LLM summarizes → `deIdentify()` scrubs PII → stored in `notes`, no `chat_history`
3. **Neither** → contact fields only, no history stored

---

## Testing

- Unit tests (mocked, fast): `python -m pytest Code/HuggingBearCode/ChatHealthyWhoAmIChat/tests/ -v`
- E2E tests (real systems): `python Code/HuggingBearCode/ChatHealthyWhoAmIChat/tests/test_e2e_record_user_details.py`
- E2E uses timestamped emails — records persist in dev DB, no teardown
- Run both before any deploy

---

## Dev Preferences

- No auto-commit — only commit when explicitly asked
- No teardown on test data — records stay in dev DB
- End-to-end tests use real systems (MongoDB, Anthropic, Pushover) — no mocks
- Keep solutions simple — no over-engineering, no speculative features
- FHIR is later — don't design for it now
