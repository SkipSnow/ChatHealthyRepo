# ChatHealthy — Project Context

## Repo
**`SkipSnow/ChatHealthyRepo`** — single monorepo for all ChatHealthy production code.

## Diagrams
Always use simple boxes and lines with white backgrounds and black text.
All diagrams must be fully visible. Never cross lines. Never place objects on top of one another.

---

## Repository Structure

```
ChatHealthyRepo/
  Code/
    ConversationalUX/         ← Layer 1: React + FastAPI (HuggingFace)
      ChatHealthyWhoAmIChat/  ← current About Us chatbot (Gradio, active)
    DataPipelines/            ← Layer 2: Azure Function App (CrewAI workflows)
    Shared/                   ← ChatHealthyMongoUtilities, common types
  Website/                    ← Static site (Cloudflare)
  Documentation/              ← Architecture docs
  Legal/
  .github/
    workflows/
      deploy-ux.yml           ← ConversationalUX → HuggingFace (on push to master)
      deploy-pipelines.yml    ← DataPipelines → Azure (on push to master)
      test.yml                ← Unit tests (on every push)
```

---

## Infrastructure

```
Cloudflare (www.chathealthy.ai)
  → Serves Website/ as static pages
  → DNS, embeds HuggingFace space via iframe

HuggingFace Space (SkipSnow/ChatHealthyWhoAmIChat)
  → Layer 1: ConversationalUX — React + FastAPI
  → Direct Anthropic SDK for chat
  → UX model: ChatGPT / Claude.ai — single session, rich inline components

Azure Function App (FindCare-AI, US East 2)
  → Layer 2: DataPipelines — CrewAI workflows
  → Python 3.12.10, pay-per-use
  → Core library: ChatHealthyLib.pipelines

MongoDB Atlas
  → Account: skip.snow@gmail.com
  → Dev cluster: ChatHealthyDB_dev
```

---

## GitHub

- **Repo**: `SkipSnow/ChatHealthyRepo`
- **Lab/experiments**: `SkipSnow/ChatHealthyLabRepo`
- CI/CD: push to master → GitHub Actions path-filtered deploy
- Secrets: HF_TOKEN, OPENAI_API_KEY, Anthropic_API_KEY, MONGO_connectionString, PUSHOVER_USER, PUSHOVER_TOKEN, AZURE_FUNCTION_APP_NAME, AZURE_FUNCTION_PUBLISH_PROFILE

---

## Architecture Decisions

### UX: One unified session
- No separate About Us app — it is one intent path within the main session
- Intent routing is invisible to the user
- Rich components (maps, charts, provider cards) render inline in the message stream
- Session transcript is a first-class output — users can share it with their doctor
- UX model: ChatGPT / Claude.ai

### Layer 1 — ConversationalUX (HuggingFace)
- React + shadcn/ui frontend
- FastAPI backend
- Direct Anthropic SDK — Claude handles routing, tool calls, response
- Tools defined in FastAPI, called by Claude during conversation

### Layer 2 — DataPipelines (Azure Functions)
- CrewAI multi-agent orchestration
- Multi-LLM: each agent uses the best model for its task
- Triggered by Layer 1 via API call for complex workflows
- Example: clinical trial search, eligibility matching, document generation

### Why CrewAI over Anthropic Agent SDK
- CrewAI supports multiple LLMs per agent — needed for cost and capability optimization

### Environments
- **Dev**: current HuggingFace Space + Azure dev slot + ChatHealthyDB_dev
- **Prod**: separate HuggingFace Space + Azure prod + ChatHealthyDB_prod (future)
- SIT/UAT added when team size justifies separate roles

### FHIR / Epic EMR
- Deferred to a later phase — plugs into Layer 2

---

## Existing Chatbot (About Us)

Currently live at `SkipSnow/ChatHealthyWhoAmIChat` on HuggingFace.
Stack: Python, Gradio 5.22, OpenAI gpt-4o-mini (chat), Claude Haiku (deIdentify + summarize).
Keep running until FindCare ConversationalUX replaces it.

---

## MongoDB Guidelines

- Use batch writes when possible
- Use connection pooling (design pending)
- Monitor and throttle — dev cluster uses shared CPU
- Tag all automated test records: `testdata: true`
- Never delete records by default — filter with `{ testdata: true }` when needed

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

1. **Verbatim**: "May we save a verbatim transcript of this conversation with your contact details?"
   - Yes → store full `chat_history` (PII intact, consent exchange included as evidence)
2. **Summary** (if verbatim declined): "May we save a de-identified summary instead?"
   - Yes → LLM summarizes → `deIdentify()` scrubs PII → stored in `notes`, no `chat_history`
3. **Neither** → contact fields only, no history stored

---

## Testing

- Unit tests (mocked): `python -m pytest Code/ConversationalUX/ChatHealthyWhoAmIChat/tests/test_record_user_details.py -v`
- E2E tests (real systems): `python Code/ConversationalUX/ChatHealthyWhoAmIChat/tests/test_e2e_record_user_details.py`
- E2E uses timestamped emails — records persist in dev DB, no teardown
- Run both before any deploy

---

## Dev Preferences

- No auto-commit — only commit when explicitly asked
- No teardown on test data — records stay in dev DB
- End-to-end tests use real systems (MongoDB, Anthropic, Pushover) — no mocks
- Keep solutions simple — no over-engineering, no speculative features
- FHIR is later — don't design for it now
- Notebooks go in ChatHealthyLabRepo, not here
