# ChatHealthy FindCare — Prompt Factory Architecture Design

**Version:** Draft 3.0  
**Date:** April 15, 2026  
**Authors:** Skip Snow (CTO/CEO), Claude Opus 4.6  
**Pattern:** Assembly Factory with Sub-Factory Composition  
**Status:** Design — human review in progress  
**Copyright:** ChatHealthy.ai LLC.

---

## 1. Problem Statement

ChatHealthy FindCare has a fully functional backend with 10 domain services, a tool router, an LLM chat loop, safety filtering, lead capture, clinical trials search, URL validation, and provider search — all accessible through a `/chat` endpoint.

None of it is reachable by users.

The current production UI (`FindCareApp.tsx`) bypasses `/chat` entirely. It calls `/classify` (vector search for specialties) and `/search` (direct MongoDB query) — two endpoints that skip every backend capability except raw provider lookup. Six funded, implemented backend capabilities are invisible to all users.

A codebase audit (April 12, 2026) identified 13 findings. An independent GPT review validated all 13. The root cause: `FindCareApp.tsx` does not use the prompt-driven chat endpoint.

This design defines the architecture that fixes this by routing all user input through a PromptAssemblyFactory — ensuring every backend capability is reachable through one unified path.

### 1.1 What Users Experience Today

| User Action | What Happens | What Should Happen |
|---|---|---|
| "Find me a bone doc in Virginia" | Vector search → specialty codes → DB query → provider cards | Same, plus AI triage, context carry-forward, specialty ranking |
| "What about in Richmond?" | New stateless /classify call — Virginia is lost | System infers Virginia from prior turn, narrows to Richmond |
| "What does a physiatrist do?" | Provider cards (no explanation) | Narrative LLM response with Markdown |
| "Tell me about clinical trials for diabetes" | Provider cards (wrong) | Clinical trials search via ClinicalTrials.gov API |
| "Who is Skip Snow?" | Provider cards (wrong) | About ChatHealthy narrative response |
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
| F3: UMLS CPT compliance | HIGH | Legal/compliance — handled by SharedServicesFactory |
| F4: ChatWindow.tsx retirement | HIGH | UX governance decision |
| F6: Regex specialty fallback | MEDIUM | Infrastructure resilience — specialty_service needs try/except |
| F9: HuggingFace mTLS | MEDIUM | Network/security layer |
| F10: Brain loop dead code | LOW | Internal tooling |
| F12: Google Maps | LOW | Feature not built |
| F13: EvaluateCare scoring | LOW | Feature not built |

---

## 2. Core Business Requirements

### 2.1 The Fundamental Requirement

**One PromptAssemblyFactory produces dynamic prompts to do all the work.** Every user input — whether it's a provider search, a conversational question, a clinical trials query, or an emergency — enters the system through a single assembly factory that combines prompt fragments from specialized sub-factories into a coherent LLM prompt carrying the user's history and context.

### 2.2 Business Requirements

| # | Requirement |
|---|---|
| BR-01 | Every user input MUST be routed through the PromptAssemblyFactory. No UI component may bypass the factory by calling backend services directly. |
| BR-02 | The system MUST carry conversation context across turns. If a user says "Virginia" then "Richmond", the system MUST infer Virginia from the prior turn. |
| BR-03 | The system MUST be able to return both structured results (provider cards) and narrative responses (Markdown text) from the same entry point. |
| BR-04 | The consent workflow MUST occur if and only if: (a) consent has not already been obtained in this session, AND (b) the system needs to or wishes to persist user data. |
| BR-05 | All URLs in LLM responses MUST be validated before reaching the user. |
| BR-06 | Emergency/safety detection MUST occur on every request. Safety is managed by the SharedServicesFactory, not as standalone middleware. |
| BR-07 | Lead capture MUST trigger when the user provides contact information, regardless of conversation context. |
| BR-08 | Clinical trials search MUST be accessible through the same entry point as provider search. |
| BR-09 | The system MUST redact UMLS CPT codes from all user-facing output. |
| BR-10 | Each epic's prompt logic MUST be independently deployable and testable. A bug in one epic's sub-factory MUST NOT affect other epics. |
| BR-11 | If the user asks 5 consecutive off-topic questions unrelated to the product's mission, the system MUST downgrade to a commodity LLM and provide minimal responses until the user returns to on-topic queries. This is a Shared Services function. |

---

## 3. Architecture

### 3.1 Pattern: Assembly Factory with Sub-Factory Composition

The architecture uses the **aircraft assembly** pattern:

- The **PromptAssemblyFactory** is the final assembly line. It knows the blueprint — where each part mounts, what forces each part creates, and in what order assembly proceeds. It does NOT understand the content of any part.
- **Sub-factories** (one per epic, plus SharedServicesFactory) each produce a finished part with a standard interface. Each sub-factory understands its own domain deeply. It knows nothing about how its output will be combined with other sub-factories' output.
- The **PromptPart interface** is the mounting spec — the standard connector between sub-factories and the assembly factory. Every sub-factory delivers a PromptPart. The assembly factory only interacts with parts through this interface.

Analogy: The wing does not understand the jet engines it carries, but must understand all the forces the engines apply. The engine factory does not know what wing it will mount on. The aircraft architect (PromptAssemblyFactory) understands the relationship of fuselage to wing to engines — the assembly blueprint.

### 3.2 Why Not a Monolithic Factory

A single factory class handling all nine epics would become a God Object. One class per epic means one failure domain, one test surface, and one deployment unit per capability cluster.

### 3.3 The PromptAssemblyFactory — What It Knows, What It Doesn't

**What the PromptAssemblyFactory knows (the blueprint):**

1. **Slot positions** — where each PromptPart mounts in the final prompt (system_context, user_intent, tool_definitions, constraints, post_processing)
2. **Assembly order** — safety check before routing, routing before content, validation after response
3. **Dependency graph** — this part needs session state, that part needs prior turn output, this part needs the routing decision
4. **Force propagation** — safety says STOP → assembly halts. Consent says OVERLAY → response gets wrapped. Off-topic says DOWNGRADE → swap LLM.

**What the PromptAssemblyFactory does NOT know (the content):**

- What a specialty code is
- How clinical trials are searched
- What makes a URL valid or invalid
- What words are profane
- What constitutes an emergency
- How provider quality is scored
- What UMLS CPT codes look like

**The minimum viable knowledge is the blueprint — slot positions, assembly order, dependency graph, and force rules. Everything else is content owned by the sub-factories. If we give the assembly factory more than this, it becomes a god object. If we give it less, it cannot assemble correctly.**

### 3.4 The PromptPart Interface — The Mounting Spec

Every sub-factory delivers a PromptPart that declares:

| Field | Purpose |
|---|---|
| `slot` | Where this part mounts (system_context, user_intent, tools, constraints, post_processing) |
| `content` | The prompt fragment (string, tool definition, or constraint) |
| `depends_on` | What this part needs from the assembly (session_state, prior_turn, routing_decision) |
| `forces` | What structural effects this part creates (HALT, OVERLAY, DOWNGRADE, NONE) |
| `priority` | Assembly priority within its slot (safety = 0, routing = 10, content = 50, validation = 90) |

The assembly factory sorts parts by slot and priority, resolves dependencies, checks for force conflicts, and assembles the final prompt. It never inspects `content`.

### 3.5 Sub-Factory Registry

| Sub-Factory | Epic | What It Produces |
|---|---|---|
| FindCarePromptFactory | FindCare | Provider search prompts, specialty routing, context carry-forward |
| NarrativePromptFactory | FindCare | Conversational question prompts, Markdown responses |
| EvaluateCarePromptFactory | Evaluate Care | Provider quality scoring prompts, clinical trials prompts |
| SharedServicesFactory | Shared Services | Safety check, consent check, URL validation, CPT redaction, off-topic detection, lead capture |

The SharedServicesFactory is special — it contributes parts to EVERY request (safety, consent checks). Other sub-factories contribute parts only when the routing decision selects their epic.

### 3.6 Cross-Cutting Concerns — Where They Live

| Concern | Owner | Slot | Why Here |
|---|---|---|---|
| Emergency/safety check | SharedServicesFactory | constraints | Safety is a domain concern, not infrastructure. The SharedServicesFactory knows what an emergency looks like. Force: HALT. |
| URL validation | SharedServicesFactory | post_processing | SharedServicesFactory knows what a valid URL is. Runs after LLM response. |
| UMLS CPT redaction | SharedServicesFactory | post_processing | Compliance filtering. SharedServicesFactory knows what CPT codes look like. |
| Consent workflow | SharedServicesFactory | post_processing | Fires only when persistence needed AND consent not yet obtained. Force: OVERLAY. |
| Off-topic downgrade | SharedServicesFactory | constraints | Tracks consecutive off-topic messages. Force: DOWNGRADE after 5. |
| Lead capture | SharedServicesFactory | post_processing | Detects contact info in user input. Triggers MongoDB write. |
| Conversation history | PromptAssemblyFactory | system_context | This IS assembly knowledge — the blueprint includes where history goes. |
| Session state | PromptAssemblyFactory | (dependency) | The assembly factory manages state that sub-factories depend on. |
| Routing/dispatch | PromptAssemblyFactory | (internal) | The assembly factory decides which sub-factory to invoke. This is blueprint logic. |

**Key change from V1:** Safety, URL validation, consent, CPT redaction, off-topic, and lead capture are NOT middleware. They are PromptParts produced by SharedServicesFactory. The assembly factory treats them the same as any other part — mount by slot, respect forces. No special-case code in the assembly factory for any cross-cutting concern.

### 3.7 Assembly Sequence

```
1. PromptAssemblyFactory receives user input + session state
2. SharedServicesFactory produces safety PromptPart (priority 0)
   → If force = HALT, assembly stops. Return safety response.
3. PromptAssemblyFactory routes to the appropriate epic sub-factory
4. Epic sub-factory produces content PromptParts (priority 50)
5. SharedServicesFactory produces constraint PromptParts (off-topic check, consent check)
6. PromptAssemblyFactory assembles all parts by slot and priority
7. Assembled prompt sent to LLM
8. LLM response received
9. SharedServicesFactory produces post_processing PromptParts (URL validation, CPT redaction, consent overlay)
10. PromptAssemblyFactory applies post-processing parts to response
11. Final response returned to UI
```

### 3.8 What This Object Will Evolve Into

It is conceived that the PromptAssemblyFactory will evolve during implementation. The initial version will be minimal — just enough blueprint knowledge to assemble correctly. As patterns emerge from real sub-factory implementations, the blueprint may grow. But every addition must be supervised by human and justified by real duplication, not assumed need.

**From a processing perspective, every attribute and function must be supervised by Skip and not auto-generated by Claude without supervision.**

---

## 4. UML Diagrams

### 4.0 Package Relationship Overview

*One diagram showing the relationship between all packages with NO objects expressed within them.*

*(To be rendered — 7 diagrams total: 1 overview + 6 package internals)*

### 4.1 Package: PromptAssemblyFactory
*(Internal class diagram + narrative explaining the assembly flow)*

### 4.2 Package: PromptPart Interface
*(Internal class diagram + narrative explaining the mounting spec)*

### 4.3 Package: SharedServicesFactory
*(Internal class diagram + narrative explaining safety, consent, URL, CPT, off-topic, lead capture)*

### 4.4 Package: FindCarePromptFactory
*(Internal class diagram + narrative explaining provider search, context carry-forward)*

### 4.5 Package: NarrativePromptFactory
*(Internal class diagram + narrative explaining conversational responses)*

### 4.6 Package: EvaluateCarePromptFactory
*(Internal class diagram + narrative explaining quality scoring, clinical trials)*

---

## 5. UML Sequence Diagrams

*(Not reviewed until package diagrams and narratives from Section 4 are complete — per human directive)*

### 5.1 Provider Search — Happy Path
### 5.2 Conversational Question — Narrative Response
### 5.3 Clinical Trials Search
### 5.4 Emergency/Safety Detection (SharedServicesFactory → HALT force)
### 5.5 Context Carry-Forward — Virginia then Richmond
### 5.6 Consent Trigger (SharedServicesFactory → OVERLAY force, conditional)
### 5.7 URL Validation on Tool Response (SharedServicesFactory → post_processing)

---

## 6. Assumptions

| # | Assumption |
|---|---|
| A-01 | The existing `/chat` endpoint and tool loop in main.py is the correct backend architecture. The PromptAssemblyFactory wraps it, not replaces it. |
| A-02 | FindCareApp.tsx will be modified to call the PromptAssemblyFactory endpoint instead of /classify and /search directly. |
| A-03 | The PromptAssemblyFactory runs server-side in the FastAPI backend, not client-side. |
| A-04 | Session state (consent status, conversation history, off-topic counter) is maintained per browser session via the existing session token mechanism. |
| A-05 | The ToolRouter and all registered tools remain unchanged. Sub-factories dispatch to them through the existing tool loop. |
| A-06 | No new LLM models are introduced. The existing GPT-4.1 chat model and GPT-4.1-mini for extraction/classification remain. A commodity LLM is used for off-topic downgrade. |

---

## 7. Known Gaps — What This Design Does NOT Cover

| # | Gap | Impact | Mitigation |
|---|---|---|---|
| G-01 | UMLS CPT code compliance (F3) | Legal pre-launch blocker | SharedServicesFactory post-processing. Separate legal workstream required. |
| G-02 | ChatWindow.tsx formal retirement (F4) | Governance debt | File a story explicitly listing each capability: retire, re-implement, or defer. |
| G-03 | Regex specialty fallback (F6) | Silent failure if vector index missing | Add try/except in SpecialtyService with substring fallback. Independent of factory. |
| G-04 | HuggingFace mTLS (F9) | Security gap in deployed environment | Implement SEC-CERTAUTH-REMOTE. Independent of factory. |
| G-05 | Schema drift between factory output and tool contracts | Silent degradation | Extend SchemaDriftDetector to cover PromptPart schemas. |
| G-06 | FindCareApp UI rendering of narrative responses | Users see only provider cards | Frontend must detect response type and render narrative Markdown when appropriate. |

---

## 8. Backlog

*(Pending — features, stories, requirements with B/T separation per v4-035)*
