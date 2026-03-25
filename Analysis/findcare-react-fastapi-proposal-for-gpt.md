# ChatHealthy FindCare — React + FastAPI Port Proposal
**For review and guidance: GPT-4**
**Date: 2026-03-24**

---

## Context

ChatHealthy is a healthcare navigation platform. The current chat window (App 2 in a 3-app architecture) is built with Python Gradio on HuggingFace Spaces. We are porting it to React + FastAPI on Azure. The Gradio app proved brittle: CSS sanitization blocked styling, error handling was opaque, and mysterious crashes occurred after several iterations. The decision is made — React + FastAPI is the right call.

---

## What the App Does

A single-session chat UI that:
1. **Finds providers** — queries a MongoDB providers database (NPI/NPPES data, Delaware and Mississippi only) by specialty, using hybrid NUCC taxonomy lookup (Haiku keyword expansion + OpenAI vector search)
2. **Identifies specialties** — explains what provider types do
3. **Searches clinical trials** — live calls to ClinicalTrials.gov v2 API
4. **Captures leads** — two-tier HIPAA consent flow, writes to MongoDB
5. **About ChatHealthy** — represents Skip Snow / ChatHealthy.AI using PDF/text documents
6. **Safety gate** — mandatory hard stop for medical emergencies (dual detection: keyword list + Haiku semantic check, IP-based 1-hour lock stored in MongoDB)

Model: gpt-4o-mini with tool-calling for all of the above.
Safety classifier: Claude Haiku (binary classification, confidence >= 0.80 threshold).

---

## Architecture Decision

**Three strict application boundaries (non-negotiable):**

| App | Host | Purpose |
|---|---|---|
| 1 — Website | Cloudflare (static) | Embeds App 2 via iframe |
| 2 — Chat | Azure (React + FastAPI) | All UX, LLM, tool calls, DB reads/writes to application DBs |
| 3 — Pipelines | Azure Functions (Python) | All PublicHealthData writes — provider ingestion, embeddings |

App 2 reads from `PublicHealthData` freely (fast provider lookups). App 2 never writes to `PublicHealthData`. App 3 owns all writes there.

---

## Proposed Target Stack

**Frontend:** React + TypeScript + Vite + shadcn/ui
- Single `ChatWindow` component
- `react-markdown` + `rehype-raw` + `rehype-sanitize` for rendering HTML+markdown in assistant messages
- Client-side history state (full history sent on every POST — stateless backend)
- `VITE_API_URL` env var pointing to FastAPI backend

**Backend:** FastAPI (Python 3.12)
- Single endpoint: `POST /api/chat`
- Request: `{ message, history: [{role, content}] }`
- Response: `{ reply, locked }` — `locked: true` disables input in React
- IP extracted from `X-Forwarded-For` header (Azure proxy)
- Direct port of all app.py logic: safety, tools, Me class, tool-calling loop

**Hosting:**
- React → Azure Static Web Apps (free tier)
- FastAPI → Azure App Service B1 (Linux, gunicorn + uvicorn workers)

**MongoDB (unchanged):**
- FrontEnd cluster: `Users`, `DeidentifiedSessions`, `Safety.emergency_incidents`
- DataPipelines cluster (read-only from App 2): `PublicHealthData.providers_staging`, `PublicHealthData.SpecialtyMetaData`

---

## Key Design Decisions Needing Your Input

### 1. In-memory IP lock cache removal
The Gradio app used a module-level dict `{ ip: expiry }` as a fast cache for the IP lock. In multi-worker App Service (4 gunicorn workers), each worker has its own dict — a message could hit a different worker than the one that set the lock.

**Proposed:** Remove the in-memory cache entirely. Every `_check_ip_lock_db` call goes to MongoDB (indexed query on `ip` + `expires_at`). The latency is < 5ms on Atlas M30.

**Question:** Is this the right call, or should we use Redis/a shared cache instead? The volume is very low (single-digit concurrent users in alpha/beta).

### 2. Blocking SDK calls in async FastAPI
The OpenAI and Anthropic SDKs are synchronous. The safety check calls Haiku synchronously, the tool-calling loop calls OpenAI synchronously.

**Proposed:** Use `anyio.to_thread.run_sync()` to wrap all blocking SDK calls in a thread pool, keeping the FastAPI event loop free.

**Question:** Is this the right pattern, or should we use the async SDK clients directly (`AsyncOpenAI`, `AsyncAnthropic`)?

### 3. `ThreadPoolExecutor` in `find_specialty_codes`
The specialty lookup runs two pipelines in parallel (regex + vector search) using `ThreadPoolExecutor`. In an already-async context, we'd call `run_in_executor` to run the whole function in a thread.

**Question:** Is nesting an executor inside a thread acceptable here, or should we restructure to `asyncio.gather` with two separate `run_in_executor` calls?

### 4. History management
Currently: React holds `Message[]` in state. On each user message, the full history array is sent to `POST /api/chat`. The backend reconstructs the OpenAI messages list from scratch.

**Question:** Is this the right pattern for a chat app at this scale? Any concern about payload size as conversations get long? Should we truncate history after N turns on the client side?

### 5. Admin unlock endpoint
Currently the unlock is a magic string in the chat (`UNLOCK:KEY`). In the React app, this would need special handling.

**Proposed:** Move it to a separate `POST /api/admin/unlock` endpoint with `Authorization: Bearer <ADMIN_UNLOCK_KEY>` header. React doesn't need to know about it — it's an out-of-band admin operation.

**Question:** Agree with this approach? Any concern about exposing this endpoint?

---

## Safety Directive (Non-Negotiable)

This is a hard system invariant — the safety gate must be the first operation in every request, before any model call:

- Dual detection: keyword list OR Haiku semantic (confidence >= 0.80) → immediate stop
- Fixed response: "**Call 911 or go to the nearest emergency room immediately. Do not wait. This chat has been suspended.**"
- IP-based 1-hour lock stored in MongoDB `Safety.emergency_incidents`
- Session lock: if `EMERGENCY_RESPONSE` appears in history, every subsequent message returns it regardless of IP state
- Failure default: if safety check errors → escalate (not pass)

No tradeoffs against cost or UX. Authority: Skip Snow.

---

## Implementation Order (Proposed)

1. FastAPI scaffold → deploy to Azure App Service → confirm `/api/health` 200
2. Port all business logic to FastAPI modules → local pytest (mocked)
3. Integration test against dev MongoDB
4. GitHub Actions `deploy-findcare-backend.yml`
5. React scaffold with Vite + shadcn/ui → confirm markdown+HTML renders correctly
6. Wire `useChat.ts` to FastAPI
7. GitHub Actions `deploy-findcare-frontend.yml` → Azure Static Web Apps
8. End-to-end UAT

---

## Questions Summary

1. In-memory IP lock cache: remove entirely (MongoDB only) vs. Redis?
2. Blocking SDKs: `anyio.to_thread.run_sync` vs. async SDK clients?
3. `ThreadPoolExecutor` nesting: acceptable or restructure to `asyncio.gather`?
4. History payload: send full history each request or truncate client-side?
5. Admin unlock: chat magic string vs. separate endpoint?
6. Anything we're missing in the proposed architecture?

---

## Files Available for Reference

- `app.py` — full Gradio source (all logic to be ported)
- `ChatHealthyMongoUtilities.py` — MongoDB connection manager
- `CLAUDE.md` — full project context and architecture rules
- `Analysis/safety-directive-2026-03-24.md` — safety policy document
