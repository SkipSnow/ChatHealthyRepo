# ChatHealthy FindCare — Prompt Factory Architecture Design

**Version:** Draft 1.0  
**Date:** April 14, 2026  
**Authors:** Skip Snow (CTO), Claude Opus 4.6, GPT-4.1 (reviewer)  
**Pattern:** Interface-First with Middleware and Experience-Driven Lifting  
**Status:** Design — implementation evidence pending

---

## 1. Problem Statement

ChatHealthy FindCare has a fully functional backend with 10 domain services, a tool router, an LLM chat loop, safety filtering, HIPAA consent triggers, lead capture, clinical trials search, URL validation, and provider search — all accessible through a `/chat` endpoint.

None of it is reachable by users.

The current production UI (`FindCareApp.tsx`) bypasses `/chat` entirely. It calls `/classify` (vector search for specialties) and `/search` (direct MongoDB query) — two endpoints that skip every backend capability except raw provider lookup. The result: six funded, implemented backend capabilities are invisible to all users.

A codebase audit (April 12, 2026) identified 13 findings. An independent GPT review validated all 13. The root cause of the most critical cluster of findings is a single architectural decision: `FindCareApp.tsx` does not use the prompt-driven chat endpoint.

This design document defines the architecture that fixes this by routing all user input through a single prompt factory — ensuring every backend capability is reachable through one unified path.

### 1.1 What Users Experience Today

| User Action | What Happens | What Should Happen |
|---|---|---|
| "Find me a bone doc in Virginia" | Vector search → specialty codes → DB query → provider cards | Same, plus AI triage, context carry-forward, specialty ranking |
| "What about in Richmond?" | New stateless /classify call — Virginia is lost | System infers Virginia from prior turn, narrows to Richmond |
| "What does a physiatrist do?" | Provider cards (no explanation) | Narrative LLM response with Markdown |
| "Tell me about clinical trials for diabetes" | Provider cards (wrong) | Clinical trials search via ClinicalTrials.gov API |
| "Who is Skip Snow?" | Provider cards (wrong) | About ChatHealthy narrative response |
| 5th message in session | Nothing | HIPAA two-tier consent trigger |
| User shares contact info | Nothing | Lead capture to MongoDB |
| AI generates fake URL | Reaches user | URL Guardian validates before rendering |

### 1.2 Audit Findings Addressed by This Design

| Finding | Priority | Status |
|---|---|---|
| F1: FindCareApp /chat bypass | CRITICAL | **Design-resolved** |
| F2: Context carry-forward regression | CRITICAL | **Design-resolved** |
| F5: URL Guardian dead-coded | HIGH | **Design-resolved** |
| F7: Specialty ranker limbo | MEDIUM | **Design-resolved** |
| F8: LLM narrative response inaccessible | MEDIUM | **Design-resolved** |
| F11: Dynamic context loading inefficiency | LOW | **Design-resolved** |

### 1.3 Audit Findings NOT Addressed by This Design

| Finding | Priority | Why Not |
|---|---|---|
| F3: UMLS CPT compliance | HIGH | Legal/compliance — handled by compliance middleware |
| F4: ChatWindow.tsx retirement | HIGH | UX governance decision |
| F6: Regex specialty fallback | MEDIUM | Infrastructure resilience — specialty_service needs try/except |
| F9: HuggingFace mTLS | MEDIUM | Network/security layer |
| F10: Brain loop dead code | LOW | Internal tooling |
| F12: Google Maps | LOW | Feature not built |
| F13: EvaluateCare scoring | LOW | Feature not built |

---

## 2. Core Business Requirements

### 2.1 The Fundamental Requirement

**One prompt does all the work via a prompt factory.** Every user input — whether it's a provider search, a conversational question, a clinical trials query, or an emergency — enters the system through a single manufactured prompt that carries the user's history and context.

### 2.2 Business Requirements

| # | Requirement |
|---|---|
| BR-01 | Every user input MUST be routed through the prompt factory. No UI component may bypass the factory by calling backend services directly. |
| BR-02 | The system MUST carry conversation context across turns. If a user says "Virginia" then "Richmond", the system MUST infer Virginia from the prior turn. |
| BR-03 | The system MUST be able to return both structured results (provider cards) and narrative responses (Markdown text) from the same entry point. |
| BR-04 | The HIPAA two-tier consent trigger MUST fire on every 5th user message regardless of which capability is handling the request. |
| BR-05 | All URLs in LLM responses MUST be validated before reaching the user. |
| BR-06 | Emergency/safety detection MUST occur before any other processing. |
| BR-07 | Lead capture MUST trigger when the user provides contact information, regardless of conversation context. |
| BR-08 | Clinical trials search MUST be accessible through the same entry point as provider search. |
| BR-09 | The system MUST redact UMLS CPT codes from all user-facing output (compliance middleware). |
| BR-10 | Each epic's prompt logic MUST be independently deployable and testable. A bug in one epic's facade MUST NOT affect other epics. |

---

## 3. Architecture

### 3.1 Pattern: Interface-First with Middleware

The architecture uses four layers:

1. **PromptFactoryInterface** — defines the contract. No implementation.
2. **Epic Facades** — one per epic. Each implements the interface. Each owns its own routing rules, tools, and output schemas.
3. **Middleware Stack** — composable chain handling cross-cutting concerns: history, context, validation, compliance, consent counter, URL validation.
4. **Dispatcher** — thin entry point that identifies the epic and delegates. No business logic.

The key constraint: **no shared base class at startup.** As patterns emerge across epic facades over time, shared logic is lifted into an abstract base — but only when proven by real duplication, not assumed.

### 3.2 Why Not a Monolithic Factory

A single factory class handling all nine epics would become a God Object — the exact maintenance and regression risk the audit identified. One class per epic means one failure domain, one test surface, and one deployment unit per capability cluster. (Source: Perplexity external audit)

### 3.3 Cross-Cutting Concerns Placement

| Concern | Location | Rationale |
|---|---|---|
| Routing/dispatch | Dispatcher | Identify epic, delegate. Nothing else. |
| Prompt history | Middleware | Applies to all epics equally |
| User context loading | Middleware | Intent-driven loading, shared logic |
| Output schema validation | Middleware | Generic validation before rendering |
| UMLS compliance filtering | Middleware | Legal requirement, post-output |
| URL validation | Middleware | Post-output, before rendering |
| HIPAA consent counter | Middleware | Session-scoped counter, fires on cadence |
| Emergency/safety check | Middleware | Pre-dispatch, blocks all other processing |
| Epic-specific routing | Epic Facade | Each epic owns its routing rules |
| Epic-specific tools | Epic Facade | Each epic registers its own tools |
| Epic-specific output schema | Epic Facade | Each epic defines its output shape |

---

## 4. UML Class Diagram

*(Pending — building from codebase analysis)*

---

## 5. UML Sequence Diagrams

### 5.1 Provider Search (Happy Path)
*(Pending)*

### 5.2 Conversational Question (Narrative Response)
*(Pending)*

### 5.3 Clinical Trials Search
*(Pending)*

### 5.4 Emergency/Safety Detection
*(Pending)*

### 5.5 Context Carry-Forward (Virginia → Richmond)
*(Pending)*

### 5.6 Consent Trigger (5th Message)
*(Pending)*

### 5.7 URL Validation on Tool Response
*(Pending)*

---

## 6. Assumptions

| # | Assumption |
|---|---|
| A-01 | The existing `/chat` endpoint and tool loop in main.py is the correct backend architecture. The prompt factory wraps it, not replaces it. |
| A-02 | FindCareApp.tsx will be modified to call the prompt factory endpoint instead of /classify and /search directly. |
| A-03 | The prompt factory runs server-side in the FastAPI backend, not client-side. |
| A-04 | Session state (HIPAA counter, conversation history) is maintained per browser session via the existing session token mechanism. |
| A-05 | The ToolRouter and all registered tools remain unchanged. The prompt factory dispatches to them through the existing tool loop. |
| A-06 | No new LLM models are introduced. The existing GPT-4.1 chat model and GPT-4.1-mini for extraction/classification remain. |

---

## 7. Known Gaps — What This Design Does NOT Cover

| # | Gap | Impact | Mitigation |
|---|---|---|---|
| G-01 | UMLS CPT code compliance (F3) | Legal pre-launch blocker | Compliance middleware filters post-output. Separate legal workstream required. |
| G-02 | ChatWindow.tsx formal retirement (F4) | Governance debt | File a story explicitly listing each capability: retire, re-implement, or defer. |
| G-03 | Regex specialty fallback (F6) | Silent failure if vector index missing | Add try/except in SpecialtyService with substring fallback. Independent of factory. |
| G-04 | HuggingFace mTLS (F9) | Security gap in deployed environment | Implement SEC-CERTAUTH-REMOTE. Independent of factory. |
| G-05 | Schema drift between factory output and tool contracts | Silent degradation | Extend SchemaDriftDetector to cover factory output schemas. |
| G-06 | FindCareApp UI rendering of narrative responses | Users see only provider cards | Frontend must detect response type and render narrative Markdown when appropriate. |

---

## 8. Backlog

*(Pending — features, stories, requirements with pytest children)*
