
# ChatHealthy FindCare
## Prompt Factory Architecture Design
Version: Draft 8.0
Date: April 15, 2026
Authors: Skip Snow (CTO/CEO), Claude Opus 4.6
With help from Perplexity using GPT 5.4 reasoning model
Pattern: Assembly Factory with Neighbor-Aware Sub-Factory Composition
Status: Design — Boss review required
Copyright: ChatHealthy.ai LLC.

## 1. Problem Statement
The existing /chat endpoint relies on long chains of sequential LLM calls — classify intent, then resolve specialty, then formulate query, then validate output. Each call adds latency, cost, and a failure point. These chains create performance bottlenecks that degrade user experience and do not scale. A well-assembled prompt that gives the LLM everything it needs in a single call — context, tools, constraints, history — eliminates the chain and solves the performance problem.
The remaining problems are downstream consequences of a radical architecture change that was not thought through end to end. ChatHealthy FindCare has a fully functional backend with 10 domain services, a tool router, an LLM chat loop, safety filtering, lead capture, clinical trials search, URL validation, and provider search — all accessible through a /chat endpoint. None of it is reachable by users. The current production UI (FindCareApp.tsx) bypasses /chat entirely. It calls /classify (vector search for specialties) and /search (direct MongoDB query) — two endpoints that skip every backend capability except raw provider lookup. Six funded, implemented backend capabilities are invisible to all users. A codebase audit (April 12, 2026) identified 13 findings. An independent GPT review validated all 13.
This design defines the architecture that fixes these problems by routing user input through a PromptAssemblyFactory — ensuring the LLM is called only when needed, with a single well-constructed prompt rather than a chain of sequential calls, and that every backend capability is reachable through one unified path.
### 1.1 What Users Experience Today
| User Action | What Happens | What Should Happen |
|---|---|---|
| "Find me a bone doc in Virginia" | Vector search, specialty codes, DB query, provider cards | Same, plus AI triage, context carry-forward, specialty ranking |
| "What about in Richmond?" | New stateless /classify call — Virginia is lost | System infers Virginia from prior turn, narrows to Richmond |
| "What does a physiatrist do?" | Provider cards (no explanation) | Narrative LLM response with Markdown |
| "Tell me about clinical trials for diabetes" | Provider cards (wrong) | Clinical trials search via ClinicalTrials.gov API |
| "Who is Skip Snow?" | Provider cards (wrong) | About ChatHealthy narrative response |
| User shares contact info | Nothing | Lead capture to MongoDB |
| AI generates fake URL | Reaches user | URL Guardian validates before rendering |


### 1.2 Audit Findings Addressed by This Design
| Finding | Priority | Status |
|---|---|---|
| F1: FindCareApp /chat bypass | CRITICAL | Design-resolved |
| F2: Context carry-forward regression | CRITICAL | Design-resolved |
| F5: URL Guardian dead-coded | HIGH | Design-resolved |
| F7: Specialty ranker limbo | MEDIUM | Design-resolved |
| F8: LLM narrative response inaccessible | MEDIUM | Design-resolved |
| F11: Dynamic context loading inefficiency | LOW | Design-resolved |


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


## 2. Core Business Requirements
### 2.1 The Fundamental Requirement
One PromptAssemblyFactory produces dynamic prompts to do all the work. Every user input — whether it is a provider search, a conversational question, a clinical trials query, or an emergency — enters the system through a single assembly factory that combines prompt fragments from specialized sub-factories into a coherent LLM prompt carrying the user's history and context.
### 2.2 Business Requirements
| # | Requirement |
|---|---|
| BR-01 | Every user input MUST be routed through the PromptAssemblyFactory. No UI component may bypass the factory by calling backend services directly. |
| BR-02 | The system MUST carry conversation context across turns. If a user says "Virginia" then "Richmond", the system MUST infer Virginia from the prior turn |
| BR-03 | The system MUST be able to return both structured results (provider cards) and narrative responses (Markdown text) from the same entry point. |
| BR-04 | The consent workflow MUST occur if and only if: (a) consent has not already been obtained in this session, AND (b) the system needs to or wishes to persist user data. |
| BR-05 | All URLs in LLM responses MUST be validated before reaching the user. |
| BR-06 | Emergency/safety detection MUST occur on every request. Safety is managed by the SharedServicesFactory, not as standalone middleware. |
| BR-07 | Lead capture MUST trigger when the user provides contact information, regardless of conversation context. |
| BR-08 | Clinical trials search MUST be accessible through the same entry point as provider search. |
| BR-09* | The system MUST determine whether an LLM is needed before invoking the PromptAssemblyFactory. If the system can handle the input with deterministic logic alone (filters, pagination, structured queries with known parameters), the PromptAssemblyFactory MUST NOT be called. The PromptAssemblyFactory is only invoked when the system cannot resolve the input without an LLM. |
| BR-10 | Each epic’s prompt logic MUST be independently deployable and testable. A bug in one epic’s sub-factory MUST NOT affect other epics. Note: Either we need a controller that understands everything, or we need each controller to understand its upstream and downstream capabilities and have proper expectations. |
| BR-11 | If the user asks 5 consecutive off-topic questions unrelated to the product’s mission, the system MUST downgrade to a commodity LLM and provide minimal responses until the user returns to on-topic queries. This is a Shared Services function. |
| BR-12 | The system must never give clinical advice. |
| BR-13 | The system must never use LLM to give answers to important questions. LLMs are used to ask the data and code the right questions. Deterministic logic must answer these questions. |
| BR-14 | The PromptAssemblyFactory CANNOT contain business logic. Each worker that assembles prompt content must publish APIs that make the PromptAssemblyFactory devoid of containing significant business logic. Only a human being (Skip) can approve any API or logic in the PromptAssemblyFactory, even if this becomes a bottleneck. |


## 3. Architecture
### 3.1 The Aircraft Assembly Pattern
There is a manufacturing metaphor that properly captures the essence of this Architecture. Call it the aircraft assembly pattern. It is exemplified by five components:
The PromptAssemblyFactory is the final assembly line. It is accountable for the blueprint. However it learns the blue print because of each’s component’s requirements and capabilities, exposed via an API — it wires everything together by interrogating the parts that give it the knowledge to do its job. So, the engine applies a great deal of force on the wing. The wing’s design must account for this. The fuselage must understand what it needs to do in order to understand the force that the wing will create in order to attach to the Wing properly. In this architecture the engine must tell the wing what it needs and the wing must tell the fuselage. And the fuselage must ask the wing for the list of things that it must know to attach to the wing properly. So each component must advertise its requirements and capabilities. Then each component based on the knowledge of what their neighbors need must support their neighbors in accordance with their needs and deliver to the PromptFactory instructions on how it fits into the flow. The Prompt factory, like the plane designer, must then be accountable for the overall flow.
So the Prompt factory is truly dumb. The components tell it what to do and it executes.
Sub-factories (one per epic, plus SharedServicesFactory) each produce finished parts with a standard interface. Each sub-factory understands its own domain deeply. It knows nothing about how its output will be combined with other sub-factories' output by the assembly factory.
However — and this is critical — each sub-factory knows enough about its neighbors to design its contributions correctly. The wing knows the engine is there and what forces the engine applies. The engine knows it mounts on a wing and what structural loads the wing can bear. Neither knows the other's internals. Each sub-factory exposes an API that tells its neighbors what it needs and what it supplies. The neighbors use these APIs to design their own contributions, but never to understand or replicate each other's logic.
The PromptPart interface is the mounting spec — the standard connector between sub-factories and the assembly factory. Every sub-factory delivers a PromptPart. The assembly factory only interacts with parts through this interface.
The PromptAssemblyFactory reads the sub-factory APIs and wires the connections based on the blueprint. The sub-factories do not call each other directly.
### 3.2 Why Not a Monolithic Factory
A single factory class handling all nine epics would become a God Object — the exact maintenance and regression risk the audit identified. One class per epic means one failure domain, one test surface, and one deployment unit per capability cluster.
### 3.3 The PromptPart Interface — The Mounting Spec
Every sub-factory delivers PromptParts through this standard interface:
| Field | Type | Purpose |
|---|---|---|
| slot | string | Where this part mounts: system_context, user_intent, tools, constraints, or post_processing |
| content | any | The prompt fragment — opaque to the assembly factory. Each prompt factory is responsible to build the dynamic elements of itself. That knowledge is nowhere else in the system. The content is almost always dynamic, at least for the part that is the user prompt. |
| priority | integer | Assembly order within its slot. 0–n where there can be no more than 20 sub components chaining the flow together. |
| forces | list | Structural effects: HALT, OVERLAY, DOWNGRADE, or NONE |
| depends_on | list | What must be resolved before this part can be produced: session_state, routing_decision, llm_response |


The assembly factory sorts parts by slot and priority, resolves dependency order, checks for force conflicts, and assembles. It never reads content.
### 3.4 What the PromptAssemblyFactory Knows (The Blueprint)
The assembly factory's intelligence is limited to exactly these six things:
1. How to get the Slot layout from each component, and the compliance to the whole in the sense that if the components contradict one another or create an order with holes in it the PromptFactory will refuse to do its job.
1. Sub-factory registry API in which all sub-factory components can register themselves with the PromptFactory and the PromptFactory can vet those registrations as sufficient, without contradiction or holes, and their capability declarations (their APIs). Not their internals.
1. Routing rules — given user input plus session state, which epic sub-factory handles this request. This is the assembly factory's one non-trivial responsibility. Routing rules are explicit and declarative. Each epic sub-factory declares what intents it handles. The assembly factory matches user intent to declarations. Ambiguous intents default to FindCare (the primary epic).
1. Dependency resolution as reported to it by the subcomponents — if a PromptPart says it depends_on safety_status, the assembly factory ensures the safety check runs first. If it depends_on llm_response, the assembly factory ensures the LLM call happens before that part runs.
1. Force wiring — if any part says force=HALT, the assembly stops. If force=DOWNGRADE, the assembly swaps the LLM model. If force=OVERLAY, the assembly wraps the response with the overlay content.
1. Session state management — the assembly factory maintains session state (last_state, last_specialty, off_topic_count, consent_status, message_history) and passes it to sub-factories that require it.
### 3.5 What the PromptAssemblyFactory Does NOT Know
The assembly factory does not know:
- What a specialty code is
- How clinical trials are searched
- What makes a URL valid or invalid
- What words are profane or dangerous
- What constitutes a medical emergency
- How provider quality is scored
- What UMLS CPT codes look like
- What an off-topic question looks like
- How consent works or when it is needed
- How lead capture detects contact information
If we give the assembly factory knowledge beyond the six items above, it becomes a god object. If we give it less, it cannot assemble correctly. The six items are the minimum viable blueprint.
### 3.6 The Routing Decision
The routing decision is the assembly factory's one non-trivial responsibility. It must classify user intent to pick the right epic sub-factory. This is the area most at risk of becoming god-like. This is mitigated by the fact that we allow each component to understand what its upstream neighbor supplies and its downstream neighbor needs.
The routing rules should be:
- Explicit and declarative, not a chain of if/else
- Each epic sub-factory declares what intents it handles as part of its API
- The assembly factory matches user intent to those declarations
- Ambiguous intents default to FindCare (the primary epic)
- Ambiguous intents are rejected and should cause the system to fail prior to deployment and without humans having to make decisions about this.
The assembly factory does not understand WHY a particular intent maps to a particular sub-factory. It only knows the mapping.
## 4. UML Package and Class Structure
### 4.0 Package Relationship Overview
This diagram shows the relationship between all packages with no internal objects expressed. It shows dependency arrows and force flows (HALT, OVERLAY, DOWNGRADE) between packages.

Figure 4.0 — Package Relationship Overview
[Design Note: Shared services is just a component. None of the other components can interface with it directly.]
### 4.1 Package: PromptAssemblyFactory
Internal class diagram showing the assembly flow, session state management, routing logic, and force propagation. The PromptAssemblyFactory contains: a SubFactoryRegistry for tracking registered sub-factories, a RoutingEngine for classifying user intent, a SessionStateManager for maintaining conversation state, and a ForceEvaluator for evaluating structural effects from PromptParts.

Figure 4.1 — PromptAssemblyFactory Internal Structure
[Design Note: What is the routing engine? Does it belong in shared services? It does not seem to fit into the design. Is this the URL validation capability? If so, doesn't it belong in shared services?]
### 4.2 Package: SharedServicesFactory
Internal class diagram showing safety detection, consent workflow, URL validation, CPT redaction, off-topic detection, and lead capture. SharedServicesFactory wraps five existing domain services (SafetyService, ConsentService, LeadService, URLGuardian, UnknownQuestionService) and provides pre-processing parts (safety_check, off_topic_detection) and post-processing parts (lead_capture, unknown_recording, consent_check, cpt_redaction, url_validation).

Figure 4.2 — SharedServicesFactory Internal Structure
[Design Note: Should this component have all these subclasses? Maybe this is an aggregation. What is inherited? Aggregation vs inheritance needs resolution.]
### 4.3 Package: FindCarePromptFactory
Internal class diagram showing three FindCare domains: find providers, find clinical trials, and find institutions. FindCarePromptFactory wraps FindProviderService (five search strategies), FindClinicalTrialService, FindInstitutionService, SpecialtyService (vector search against NUCC embeddings), and HomeopathicResolver.

Figure 4.3 — FindCarePromptFactory Internal Structure
[Design Note: FindCare finds providers, clinical trials, AND institutions — three classes. FindProvider should be one class, FindClinicalTrial and FindInstitution are the others. Aggregation not inheritance.]
### 4.4 Package: NarrativePromptFactory (Deprecated as Standalone)
NarrativePromptFactory is no longer a standalone factory. The principle is that code answers questions, not AI. The idea that AI is producing anything other than structured output, allowing deterministic logic to find the answer, seems contrary to our fundamental architecture and our one and only architectural policy.
Narrative is now a SIDE TRIP capability within each factory. When a user asks a clarifying question relevant to a factory's domain (e.g., "What does a physiatrist do?" during a FindCare flow), the factory handles it as a narrative side trip — not by routing to a separate factory. See Section 7 for activity diagrams.

Figure 4.4 — NarrativePromptFactory Deprecation Notice
[Design Note: NarrativePromptFactory — principle is code answers questions, not AI. Narrative is now a SIDE TRIP capability within each factory (see Section 7).]
### 4.5 Package: EvaluateCarePromptFactory
Internal class diagram showing provider quality evaluation, clinical trials evaluation, and institution evaluation. EvaluateCarePromptFactory wraps EvaluateProviderService, EvaluateClinicalTrialService, and EvaluateInstitutionService (missing — flagged as architectural gap).

Figure 4.5 — EvaluateCarePromptFactory Internal Structure
[Design Note: EvaluateCare is the preferred model. FindCare must be consistent with it. Both are missing 'evaluate institution'. This is an architectural bug that must be resolved: FindCare and EvaluateCare must have consistent structures.]
## 5. Sub-Factory Capability Declarations
This section defines each sub-factory completely: what it does, what real code it wraps, what capabilities it has, what it supplies to neighbors, what it requires from neighbors, what forces it creates, and what forces it responds to.
From a processing perspective, every attribute and function must be supervised by Skip and not auto-generated by Claude without supervision. This object will evolve during implementation.
Sequence diagrams are included inline after each factory's capability tables.
### 5.1 SharedServicesFactory
Domain: Cross-cutting concerns that apply to every request regardless of which epic handles it. Lives in the Shared Services epic. This is the one sub-factory that runs on EVERY request — both before and after the epic sub-factory.
Real code it wraps:
- SafetyService (safety_service.py) — emergency detection via three-gate AI classifier (body location + acute onset + life-threat), IP locking, audit trail
- ConsentService (consent_service.py) — HIPAA two-tier consent, de-identification via Claude Haiku
- LeadService (lead_service.py) — contact capture to MongoDB
- URLGuardian (url_guardian.py) — three-stage URL validation (HEAD check, AI content verify, Google search correction)
- UnknownQuestionService (unknown_question_service.py) — off-topic classification and recording
- CPT redaction (not yet implemented — compliance requirement)
#### Capabilities
| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| safety_check | constraints | 0 | HALT | Checks if message is an emergency (SafetyService.is_emergency — three-gate: body location + acute onset + life-threat). Also checks IP lock. If triggered, force=HALT and assembly stops immediately. Returns emergency response with 911 guidance. |
| off_topic_detection | constraints | 10 | DOWNGRADE | Tracks consecutive off-topic messages using session state. After 5 consecutive off-topic questions, force=DOWNGRADE — the assembly factory swaps GPT-4.1 for a commodity LLM. Resets when user returns to on-topic queries. |
| consent_check | post_processing | 80 | OVERLAY | Checks two conditions: (a) system needs to persist user data (lead capture, transcript storage), AND (b) consent has not yet been obtained in this session. If both true, force=OVERLAY — consent dialog wraps the normal response. |
| lead_capture | post_processing | 70 | NONE | Detects contact information in user input (email, phone, name). Triggers LeadService.record_user_details to MongoDB. Respects consent tier for de-iden |
| unknown_recording | post_processing | 75 | NONE | If the routing decision classified the question as unanswerable, records the question via UnknownQuestionService for product improvement. |
| cpt_redaction | post_processing | 85 | NONE | Scans LLM response for UMLS CPT codes and redacts them. Compliance requirement — CPT codes cannot appear in consumer-facing output. |
| url_validation | post_processing | 90 | NONE | Validates all URLs in LLM response using URLGuardian. Three stages: HEAD reachability check, AI content verification via Claude Haiku, Google Custom Search for broken URL correction. Defangs links that cannot be validated. |


#### What SharedServicesFactory Supplies (API to Neighbors)
| Output | Type | Description |
|---|---|---|
| safety_status | bool | Is this request safe to process? |
| ip_locked | bool | Is this IP address locked out? |
| consent_status | string | "obtained" or "needed" or "not_needed" |
| off_topic_count | integer | Consecutive off-topic message count |
| validated_response | string | LLM response text with URLs validated and CPT codes redacted |


#### What SharedServicesFactory Requires (API from Neighbors)
| Input | Type | Source | Description |
|---|---|---|---|
| user_message | string | PromptAssemblyFactory | Raw user input for safety check and off-topic detection |
| user_ip | string | PromptAssemblyFactory | IP address for IP locking |
| session_state | dict | PromptAssemblyFactory | consent_obtained, off_topic_count, message_count |
| llm_response | string | PromptAssemblyFactory | LLM output text — only for post_processing capabilities |
| routing_decision | string | PromptAssemblyFactory | Which epic was selected — for determining if question was on-mission |


#### Forces Created by SharedServicesFactory
| Force | When | Effect on Assembly |
|---|---|---|
| HALT | Emergency detected or IP locked | Assembly stops immediately. Return emergency response. No epic sub-factory runs. |
| OVERLAY | Consent needed and not yet obtained | Normal response produced by epic sub-factory, then consent dialog overlaid on top. Epic runs normally. |
| DOWNGRADE | 5 or more consecutive off-topic messages | Assembly swaps GPT-4.1 for commodity LLM. Epic sub-factory still runs but with degraded model quality. |


#### Forces SharedServicesFactory Responds To
SharedServicesFactory does not respond to forces from other sub-factories. It is always invoked. It is the first and last to run.
#### SharedServicesFactory Sequence Diagrams

Figure 5.1a — Emergency/Safety Detection (SharedServicesFactory HALT force)

Figure 5.1b — Consent Trigger (SharedServicesFactory OVERLAY force)

Figure 5.1c — URL Validation on Tool Response (SharedServicesFactory post_processing)
### 5.2 FindCarePromptFactory
Domain: Provider search, clinical trials search, institution search, specialty resolution, geographic context carry-forward. The core FindCare epic. This is the primary epic — ambiguous intents default here.
Real code it wraps:
- FindCareService (provider_search_service.py) — five search strategies: NPI exact lookup, provider name search, specialty codes direct filter, specialty query (vector-to-taxonomy), and county fallback
- SpecialtyService (specialty_service.py) — AI vector search against NUCC specialty embeddings using text-embedding-3-large and cosine similarity
- SpecialtyClassifier (specialty_classifier.py) — classifies specialties on can_prescribe and homeopathic dimensions via GPT-4.1-mini
- SpecialtyRanker (specialty_ranker.py) — reorders specialty options by relevance to user query via GPT-4.1-mini
- HomeopathicResolver (homeopathic_resolver.py) — evaluates alternative medicine specialties for compliance with user search query via Claude Haiku
#### Capabilities
| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| provider_search_prompt | user_intent | 50 | NONE | Builds prompt fragment for provider search including specialty codes, state, city, county, pagination context. Registers find_providers and find_specialty_codes tools with the tool router. |
| context_carry_forward | system_context | 40 | NONE | Injects prior turn's state, specialty, and city into the system context so conversational follow-ups work. "What about in Richmond?" resolves Virginia |
| specialty_resolution | tools | 50 | NONE | Provides vector search for specialty codes. Includes homeopathic resolution and specialty ranking as part of the search flow. |
| filter_options | post_processing | 60 | NONE | Extracts specialty filter options from search results for the UI filter panel. Includes can_prescribe and homeopathic flags per specialty. |


#### What FindCarePromptFactory Supplies (API to Neighbors)
| Output | Type | Description |
|---|---|---|
| search_results | dict | Providers list, total_count, has_more flag, pagination cursor (after_npi) |
| specialty_options | list | Specialty codes for UI filter panel with can_prescribe and homeopathic flags |
| search_context | dict | last_state, last_city, last_specialty — for context carry-forward across turns |
| response_type | string | "provider_cards" — tells the UI to render structured provider cards, not narrative text |


#### What FindCarePromptFactory Requires (API from Neighbors)
| Input | Type | Source | Description |
|---|---|---|---|
| safety_status | bool | SharedServicesFactory | Do not build prompt if HALT |
| session_state | dict | PromptAssemblyFactory | last_state, last_city, last_specialty, message_history |
| user_message | string | PromptAssemblyFactory | Raw user input |
| off_topic_count | integer | SharedServicesFactory | If DOWNGRADE, skip expensive vector search |


#### Forces Created by FindCarePromptFactory
FindCarePromptFactory creates no forces. It is a content producer.
#### Forces FindCarePromptFactory Responds To
| Force | From | My Response |
|---|---|---|
| HALT | SharedServicesFactory | Do not run. Do not build prompt. Do not call MongoDB or OpenAI. |
| DOWNGRADE | SharedServicesFactory | Skip vector search (expensive). Use simple text match fallback. Return fewer results. |


#### FindCarePromptFactory Sequence Diagrams

Figure 5.2a — Provider Search — Happy Path

Figure 5.2b — Conversational Question — Narrative Side Trip

Figure 5.2c — Context Carry-Forward — Virginia then Richmond
### 5.3 NarrativePromptFactory (Deprecated as Standalone)
NarrativePromptFactory is deprecated as a standalone factory. Its capability tables from V7 are preserved here for reference during the transition to narrative side trips within each factory.
Domain: Conversational questions that do not produce provider cards. Examples: "What does a physiatrist do?" "Who is Skip Snow?" "What is ChatHealthy?" These are questions that need a narrative Markdown response, not structured data.
Real code it wraps:
- AboutService (about_service.py) — Skip Snow biography, ChatHealthy company info, Anthropic principles. Static content loaded from the me/ directory.
- The general LLM chat path — system prompt plus user message produces narrative Markdown. No tool calls involved.
#### Capabilities (Reference — Migrating to Side Trips)
| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| narrative_prompt | user_intent | 50 | NONE | Builds prompt for open-ended conversational response. The LLM returns Markdown text, not structured provider data. |
| about_context | system_context | 45 | NONE | When intent is "about" (Skip Snow, ChatHealthy, Anthropic), injects the relevant About content into the system context. |


#### What NarrativePromptFactory Supplies (API to Neighbors)
| Output | Type | Description |
|---|---|---|
| narrative_text | string | Markdown response — not structured data |
| response_type | string | "narrative" — tells the UI to render Markdown, not provider cards |
| about_context | dict | Skip Snow bio, ChatHealthy company info — only when intent is "about" |


#### What NarrativePromptFactory Requires (API from Neighbors)
| Input | Type | Source | Description |
|---|---|---|---|
| safety_status | bool | SharedServicesFactory | Do not run if HALT |
| user_message | string | PromptAssemblyFactory | Raw user input |
| routing_decision | string | PromptAssemblyFactory | Confirmation that this is a narrative question, not a search |
| session_state | dict | PromptAssemblyFactory | message_history for conversational continuity |


#### Forces Created by NarrativePromptFactory
NarrativePromptFactory creates no forces. It is a content producer.
#### Forces NarrativePromptFactory Responds To
| Force | From | My Response |
|---|---|---|
| HALT | SharedServicesFactory | Do not run. |
| DOWNGRADE | SharedServicesFactory | Use commodity LLM for response instead of GPT-4.1. |


### 5.4 EvaluateCarePromptFactory
Domain: Provider quality evaluation, clinical trials search, provider detail lookup, institution evaluation. The Evaluate Care epic. This sub-factory handles requests that go beyond finding a provider — evaluating provider quality, searching for clinical trials, and looking up detailed provider information.
Real code it wraps:
- EvaluateCareFacade (evaluate_care_facade.py) — public interface wrapping clinical trials and provider detail services
- ClinicalTrialsService (clinical_trials_service.py) — searches ClinicalTrials.gov for recruiting trials, optionally enriches with travel time via Google Routes API
- ProviderDetailService (provider_detail_service.py) — NPI Registry lookup plus constructs external research URLs for Healthgrades, NPI Registry, and state medical board
#### Capabilities
| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| clinical_trials_prompt | user_intent | 50 | NONE | Builds prompt for clinical trials search. Registers search_clinical_trials tool with the tool router. Queries ClinicalTrials.gov API. |
| provider_detail_prompt | user_intent | 50 | NONE | Builds prompt for provider quality lookup. Registers lookup_provider_external tool. Queries NPI Registry API. |
| quality_context | system_context | 45 | NONE | Injects quality scoring context (measures, weights, explainability framework) into the system prompt when the intent is evaluate. |


#### What EvaluateCarePromptFactory Supplies (API to Neighbors)
| Output | Type | Description |
|---|---|---|
| trials_results | dict | Clinical trial list with NCT IDs, phases, locations, eligibility, travel info |
| provider_details | dict | NPI details, research URLs (Healthgrades, NPI Registry, state board), quality scores |
| response_type | string | "trials" or "provider_detail" or "quality_score" — tells the UI what to render |


#### What EvaluateCarePromptFactory Requires (API from Neighbors)
| Input | Type | Source | Description |
|---|---|---|---|
| safety_status | bool | SharedServicesFactory | Do not run if HALT |
| user_message | string | PromptAssemblyFactory | Raw user input |
| routing_decision | string | PromptAssemblyFactory | Confirmation that this is an evaluate care request |
| session_state | dict | PromptAssemblyFactory | selected_providers from FindCare handoff — which providers the user selected for evaluation |


#### Forces Created by EvaluateCarePromptFactory
EvaluateCarePromptFactory creates no forces. It is a content producer.
#### Forces EvaluateCarePromptFactory Responds To
| Force | From | My Response |
|---|---|---|
| HALT | SharedServicesFactory | Do not run. |
| DOWNGRADE | SharedServicesFactory | Skip quality scoring (expensive). Return basic provider details only. |


#### EvaluateCarePromptFactory Sequence Diagrams

Figure 5.4a — Clinical Trials Search
### 5.5 PromptAssemblyFactory — The Blueprint
Domain: Assembly only. Knows the blueprint. Does not produce content. Deliberately dumb.
#### What the PromptAssemblyFactory Knows
1. Slot layout — the order of assembly slots: system_context first, then constraints, then user_intent, then tools, then the LLM call happens, then post_processing runs on the LLM response.
1. Sub-factory registry — which sub-factories exist, and their capability declarations (their supply/require/force APIs). Not their internals.
1. Routing rules — given user input plus session state, which epic sub-factory handles this request. Each epic sub-factory declares what intents it handles. The assembly factory matches user intent to those declarations. Ambiguous intents default to FindCare.
1. Dependency resolution — SharedServicesFactory.safety_status must resolve before any epic sub-factory runs. Post-processing parts depend on the LLM response existing.
1. Force wiring — HALT from SharedServicesFactory means skip all epic sub-factories and return the emergency response. DOWNGRADE means swap the LLM model to a commodity model. OVERLAY means wrap the final response with a consent dialog.
1. Session state management — the assembly factory maintains session state (last_state, last_specialty, off_topic_count, consent_status, message_history) and passes it to sub-factories that require it.
#### What the PromptAssemblyFactory Does NOT Know
- What a specialty code is
- How clinical trials are searched
- What makes a URL valid or invalid
- What words are profane or dangerous
- What constitutes a medical emergency
- How provider quality is scored
- What UMLS CPT codes look like
- What an off-topic question looks like
- How consent works or when it is needed
- How lead capture detects contact information
If we give the assembly factory knowledge beyond the six items above, it becomes a god object. If we give it less, it cannot assemble correctly. The six items are the minimum viable blueprint.
#### The Assembly Sequence
Step 1: Receive user input plus session state from the UI.
Step 2: Ask SharedServicesFactory for safety_check (priority 0, slot=constraints). If force equals HALT, return the emergency response immediately. Stop. No epic sub-factory runs.
Step 3: Route. Determine which epic sub-factory handles this request. The routing decision is the assembly factory's one non-trivial responsibility. Each sub-factory declares what intents it handles. The assembly factory matches user intent to those declarations.
Step 4: Ask SharedServicesFactory for off_topic_detection (priority 10, slot=constraints). If force equals DOWNGRADE, note the LLM swap for step 7.
Step 5: Ask the selected epic sub-factory for its PromptParts (priority 50). The sub-factory reads its required inputs from the assembly (session_state, safety_status, routing_decision). The sub-factory returns PromptParts with the content it produced. The assembly factory does not inspect that content.
Step 6: Collect all PromptParts from all sources. Sort by slot, then by priority within each slot.
Step 7: Assemble the final prompt from the sorted parts. Send the assembled prompt to the LLM. If DOWNGRADE was noted in step 4, send to the commodity LLM instead of GPT-4.1.
Step 8: Receive the LLM response.
Step 9: Ask SharedServicesFactory for post_processing parts (priorities 70 through 90). These include lead capture, unknown recording, consent check, CPT redaction, and URL validation. Apply each post-processing part to the LLM response in priority order.
Step 10: If the consent check produced force=OVERLAY, wrap the processed response with the consent dialog.
Step 11: Update session state. Save last_state, last_specialty, off_topic_count, consent_status, and message_history for the next turn.
Step 12: Return the final response to the UI. The response includes response_type (from the epic sub-factory) so the UI knows whether to render provider cards, narrative Markdown, clinical trials, or a quality report.
## 6. Neighbor Awareness Matrix
This matrix shows what each sub-factory knows about its neighbors. The rule: each sub-factory knows its neighbors' interfaces (what they supply, what forces they create) but not their implementations (how they produce those outputs). The wing knows the engine's thrust vector but not the combustion chamber design.
| Sub-Factory | Knows About SharedServices | Knows About FindCare | Knows About Narrative | Knows About EvaluateCare |
|---|---|---|---|---|
| SharedServicesFactory | (self) | Knows FindCare produces URLs in its provider results that need validation | Knows Narrative produces text that may contain URLs | Knows EvaluateCare produces URLs (research site links) that need validation |
| FindCarePromptFactory | Knows safety_status and off_topic_count affect whether it runs and how | (self) | Does not know about Narrative | Knows EvaluateCare may receive its selected_providers for quality evaluation |
| NarrativePromptFactory | Knows safety_status affects whether it runs | Does not know about FindCare | (self) | Does not know about EvaluateCare |
| EvaluateCarePromptFactory | Knows safety_status affects whether it runs | Knows FindCare provides selected_providers for quality evaluation | Does not know about Narrative | (self) |
| PromptAssemblyFactory | Knows SharedServices runs first (safety) and last (post-processing). Knows its three forces (HALT, OVERLAY, DOWNGRADE). | Knows FindCare handles search intents. Knows it is the default for ambiguous intents. | Knows Narrative handles question/about intents. | Knows EvaluateCare handles quality/trials intents. |


## 7. Activity Diagrams — Narrative Side Trips
Each factory may need to handle clarifying questions from users. When a user asks "What does a physiatrist do?" during a FindCare flow, this is a narrative side trip — not a separate factory. The activity diagram shows how each factory handles the branching between structured queries and clarifying questions.
The flow for each factory is:
- User input arrives → Is this a structured query? → YES → normal factory path
- NO → Is this a clarifying question relevant to this factory's domain? → YES → narrative side trip → return to factory flow
- NO → off-topic → downgrade
These activity diagrams are flagged as "candidate for lifting" — if the patterns turn out identical during implementation, the narrative side trip logic should be extracted into a shared base class or mixin.
### 7.1 FindCare Narrative Side Trip

Figure 7.1 — FindCare Narrative Side Trip Activity Diagram
Examples of FindCare narrative side trips: "What does a physiatrist do?", "What is an NPI number?", "What counties are in Virginia?". These are clarifying questions within the FindCare domain that should be answered by deterministic logic with domain context, not by routing to a separate narrative factory.
### 7.2 EvaluateCare Narrative Side Trip

Figure 7.2 — EvaluateCare Narrative Side Trip Activity Diagram
Examples of EvaluateCare narrative side trips: "What is a Phase 3 trial?", "How is provider quality scored?", "What does Healthgrades measure?". These are clarifying questions within the EvaluateCare domain.
Candidate for lifting: If FindCare and EvaluateCare narrative side trip patterns are identical during implementation, extract the branching logic into a shared NarrativeSideTripMixin or base class method.
## 8. Assumptions
| # | Assumption |
|---|---|
| A-01 | The existing /chat endpoint and tool loop in main.py is the correct backend architecture. The PromptAssemblyFactory wraps it, not replaces it. |
| A-02 | FindCareApp.tsx will be modified to call the PromptAssemblyFactory endpoint instead of /classify and /search directly. |
| A-03 | The PromptAssemblyFactory runs server-side in the FastAPI backend, not client-side. |
| A-04 | Session state (consent status, conversation history, off-topic counter) is maintained per browser session via the existing session token mechanism. |
| A-05 | The ToolRouter and all registered tools remain unchanged. Sub-factories dispatch to them through the existing tool loop. |
| A-06 | No new LLM models are introduced. The existing GPT-4.1 chat model and GPT-4.1-mini for extraction/classification remain. A commodity LLM is used for off-topic downgrade. |


## 9. Known Gaps — What This Design Does NOT Cover
| # | Gap | Impact | Mitigation |
|---|---|---|---|
| G-01 | UMLS CPT code compliance (F3) | Legal pre-launch blocker | SharedServicesFactory post-processing. Separate legal workstream required. |
| G-02 | ChatWindow.tsx formal retirement (F4) | Governance debt | File a story explicitly listing each capability: retire, re-implement, or defer. |
| G-03 | Regex specialty fallback (F6) | Silent failure if vector index missing | Add try/except in SpecialtyService with substring fallback. Independent of factory. |
| G-04 | HuggingFace mTLS (F9) | Security gap in deployed environment | Implement SEC-CERTAUTH-REMOTE. Independent of factory. |
| G-05 | Schema drift between factory output and tool contracts | Silent degradation | Extend SchemaDriftDetector to cover PromptPart schemas. |
| G-06 | FindCareApp UI rendering of narrative responses | Users see only provider cards | Frontend must detect response_type and render narrative Markdown when appropriate. |


## 10. Backlog
Pending — features, stories, requirements with B/T separation per engineering rule v4-035. Will be created after Boss approves the architecture.
Note: I will not review sequence diagrams until we agree on the object model and the architecture.