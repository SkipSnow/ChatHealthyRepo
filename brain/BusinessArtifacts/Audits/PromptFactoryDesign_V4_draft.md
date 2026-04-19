# ChatHealthy FindCare — Prompt Factory Architecture Design

**Version:** Draft 4.0  
**Date:** April 15, 2026  
**Authors:** Skip Snow (CTO/CEO), Claude Opus 4.6  
**Pattern:** Assembly Factory with Neighbor-Aware Sub-Factory Composition  
**Status:** Design — human review required  
**Copyright:** ChatHealthy.ai LLC.

---

## 1. Problem Statement

*(Unchanged from V3 — see PromptFactoryDesign_V3_draft.md for full problem statement, audit findings, and user experience table.)*

---

## 2. Core Business Requirements

*(Unchanged from V3 — see PromptFactoryDesign_V3_draft.md for BR-01 through BR-11.)*

---

## 3. Architecture

### 3.1 Pattern: Assembly Factory with Neighbor-Aware Sub-Factories

Each sub-factory knows enough about its neighbors to design its contributions correctly, but does not know how its neighbors produce their contributions. Like an aircraft: the wing knows the engine is there and what forces it applies, but not how combustion works. The engine knows it mounts on a wing and what structural loads the wing can bear, but not how the wing generates lift.

Each sub-factory exposes an API that tells its neighbors:
- **What I supply** — what outputs I produce
- **What I require** — what inputs I need from you
- **What forces I create** — what structural effects my output has on the assembly
- **What forces I respond to** — what your forces mean for my behavior

The PromptAssemblyFactory reads these APIs and wires the connections. It is deliberately dumb — it knows the blueprint (slot positions, assembly order, wiring rules) but never the content.

### 3.2 The PromptPart Interface

Every sub-factory delivers PromptParts through this standard interface:

```
PromptPart:
    slot: str           — where this mounts (system_context | user_intent | tools | constraints | post_processing)
    content: Any        — the prompt fragment (the sub-factory's output — opaque to the assembly factory)
    priority: int       — assembly order within slot (0 = first, 100 = last)
    forces: list[Force] — structural effects (HALT | OVERLAY | DOWNGRADE | NONE)
    depends_on: list[str] — what I need resolved before I can produce (session_state | routing_decision | llm_response)
```

The assembly factory sorts parts by slot and priority, resolves dependency order, checks for force conflicts, and assembles. It never reads `content`.

---

## 4. Sub-Factory Capability Declarations

### 4.1 SharedServicesFactory

**Domain:** Cross-cutting concerns that apply to every request regardless of which epic handles it. Lives in the Shared Services epic.

**Real code it wraps:**
- SafetyService (safety_service.py) — emergency detection, IP locking
- ConsentService (consent_service.py) — HIPAA two-tier consent, de-identification
- LeadService (lead_service.py) — contact capture
- URLGuardian (url_guardian.py) — URL validation and sanitization
- UnknownQuestionService (unknown_question_service.py) — off-topic classification
- CPT redaction (not yet implemented — compliance middleware)

#### Capabilities

| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| safety_check | constraints | 0 | HALT | Checks if message is emergency (SafetyService.is_emergency). If true, force=HALT — assembly stops, returns emergency response. Also checks IP lock. |
| consent_check | post_processing | 80 | OVERLAY | Checks if system needs to persist data AND consent not yet obtained. If true, force=OVERLAY — consent dialog wraps response. |
| url_validation | post_processing | 90 | NONE | Validates all URLs in LLM response (URLGuardian.guard_text). Corrects or defangs broken links. |
| cpt_redaction | post_processing | 85 | NONE | Scans LLM response for UMLS CPT codes, redacts them. Compliance requirement. |
| lead_capture | post_processing | 70 | NONE | Detects contact info in user input. Triggers LeadService.record_user_details. |
| off_topic_detection | constraints | 10 | DOWNGRADE | Tracks consecutive off-topic messages. After 5, force=DOWNGRADE — assembly swaps to commodity LLM. |
| unknown_recording | post_processing | 75 | NONE | If question classified as unanswerable, records via UnknownQuestionService. |

#### What I Supply (API to neighbors)

```
SharedServicesFactory.supplies:
    safety_status:       bool     — is this request safe to process?
    ip_locked:           bool     — is this IP locked out?
    consent_status:      str      — "obtained" | "needed" | "not_needed"
    off_topic_count:     int      — consecutive off-topic message count
    validated_response:  str      — LLM response with URLs validated and CPT codes redacted
```

#### What I Require (API from neighbors)

```
SharedServicesFactory.requires:
    user_message:        str      — raw user input (for safety check, off-topic detection)
    user_ip:             str      — IP address (for IP locking)
    session_state:       dict     — consent_obtained, off_topic_count, message_count
    llm_response:        str      — LLM output text (for URL validation, CPT redaction — post_processing only)
    routing_decision:    str      — which epic was selected (for off-topic: was this on-mission?)
```

#### What Forces I Create

| Force | When | Effect on Assembly |
|---|---|---|
| HALT | Emergency detected or IP locked | Assembly stops immediately. Return emergency response. No epic sub-factory runs. |
| OVERLAY | Consent needed and not obtained | Normal response produced, then consent dialog overlaid. Epic sub-factory runs normally. |
| DOWNGRADE | 5+ consecutive off-topic messages | Assembly swaps GPT-4.1 for commodity LLM. Epic sub-factory still runs but with degraded model. |

#### What Forces I Respond To

SharedServicesFactory does not respond to forces from other sub-factories. It is always invoked. It is the first and last to run.

---

### 4.2 FindCarePromptFactory

**Domain:** Provider search, specialty resolution, geographic context carry-forward. The core FindCare epic.

**Real code it wraps:**
- FindCareService (provider_search_service.py) — five search strategies
- SpecialtyService (specialty_service.py) — vector search for NUCC specialties
- SpecialtyClassifier (specialty_classifier.py) — can_prescribe, homeopathic flags
- SpecialtyRanker (specialty_ranker.py) — relevance-based reordering
- HomeopathicResolver (homeopathic_resolver.py) — alternative medicine compliance

#### Capabilities

| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| provider_search_prompt | user_intent | 50 | NONE | Builds prompt fragment for provider search with specialty codes, state, city, pagination context. |
| context_carry_forward | system_context | 40 | NONE | Injects prior turn's state, specialty, city into system context so "What about Richmond?" works. |
| specialty_resolution | tools | 50 | NONE | Registers find_providers and find_specialty_codes tools with the tool router. |
| filter_options | post_processing | 60 | NONE | Extracts specialty filter options from search results for UI filter panel. |

#### What I Supply (API to neighbors)

```
FindCarePromptFactory.supplies:
    search_results:      dict     — providers, total_count, has_more, pagination cursor
    specialty_options:    list     — specialty codes for filter panel
    search_context:      dict     — last_state, last_city, last_specialty (for context carry-forward)
    response_type:       str      — "provider_cards" (structured, not narrative)
```

#### What I Require (API from neighbors)

```
FindCarePromptFactory.requires:
    safety_status:       bool     — from SharedServicesFactory (don't build prompt if HALT)
    session_state:       dict     — last_state, last_city, last_specialty, message_history
    user_message:        str      — raw user input
    off_topic_count:     int      — from SharedServicesFactory (if DOWNGRADE, don't do expensive vector search)
```

#### What Forces I Create

FindCarePromptFactory creates no forces. It is a content producer.

#### What Forces I Respond To

| Force | From | My Response |
|---|---|---|
| HALT | SharedServicesFactory | Don't run. Don't build prompt. Don't call MongoDB or OpenAI. |
| DOWNGRADE | SharedServicesFactory | Skip vector search (expensive). Use simple text match fallback. Return fewer results. |

---

### 4.3 NarrativePromptFactory

**Domain:** Conversational questions that don't produce provider cards. "What does a physiatrist do?" "Who is Skip Snow?" "What is ChatHealthy?"

**Real code it wraps:**
- AboutService (about_service.py) — Skip Snow bio, ChatHealthy company info
- The general LLM chat path (no tool calls, just system prompt + user message → narrative Markdown)

#### Capabilities

| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| narrative_prompt | user_intent | 50 | NONE | Builds prompt for open-ended conversational response. Returns Markdown, not provider cards. |
| about_context | system_context | 45 | NONE | Injects About content (Skip Snow, ChatHealthy, Anthropic principles) when intent is "about." |

#### What I Supply (API to neighbors)

```
NarrativePromptFactory.supplies:
    narrative_text:      str      — Markdown response (not structured data)
    response_type:       str      — "narrative" (tells UI to render Markdown, not cards)
    about_context:       dict     — Skip Snow bio, ChatHealthy info (if about intent)
```

#### What I Require (API from neighbors)

```
NarrativePromptFactory.requires:
    safety_status:       bool     — from SharedServicesFactory
    user_message:        str      — raw user input
    routing_decision:    str      — confirmation that this is a narrative question, not a search
    session_state:       dict     — message_history (for conversational continuity)
```

#### What Forces I Create

NarrativePromptFactory creates no forces. It is a content producer.

#### What Forces I Respond To

| Force | From | My Response |
|---|---|---|
| HALT | SharedServicesFactory | Don't run. |
| DOWNGRADE | SharedServicesFactory | Use commodity LLM for response. |

---

### 4.4 EvaluateCarePromptFactory

**Domain:** Provider quality evaluation, clinical trials search, provider detail lookup. The Evaluate Care epic.

**Real code it wraps:**
- EvaluateCareFacade (evaluate_care_facade.py) — wraps clinical trials + provider details
- ClinicalTrialsService (clinical_trials_service.py) — ClinicalTrials.gov search
- ProviderDetailService (provider_detail_service.py) — NPI Registry + research URLs

#### Capabilities

| Capability | Slot | Priority | Force | Description |
|---|---|---|---|---|
| clinical_trials_prompt | user_intent | 50 | NONE | Builds prompt for clinical trials search. Registers search_clinical_trials tool. |
| provider_detail_prompt | user_intent | 50 | NONE | Builds prompt for provider quality lookup. Registers lookup_provider_external tool. |
| quality_context | system_context | 45 | NONE | Injects quality scoring context (measures, weights, explainability) into system prompt. |

#### What I Supply (API to neighbors)

```
EvaluateCarePromptFactory.supplies:
    trials_results:      dict     — clinical trial list with NCT IDs, phases, locations, travel info
    provider_details:    dict     — NPI details, research URLs, quality scores
    response_type:       str      — "trials" | "provider_detail" | "quality_score"
```

#### What I Require (API from neighbors)

```
EvaluateCarePromptFactory.requires:
    safety_status:       bool     — from SharedServicesFactory
    user_message:        str      — raw user input
    routing_decision:    str      — confirmation that this is an evaluate care request
    session_state:       dict     — selected_providers (from FindCare handoff)
```

#### What Forces I Create

EvaluateCarePromptFactory creates no forces. It is a content producer.

#### What Forces I Respond To

| Force | From | My Response |
|---|---|---|
| HALT | SharedServicesFactory | Don't run. |
| DOWNGRADE | SharedServicesFactory | Skip quality scoring (expensive). Return basic details only. |

---

### 4.5 PromptAssemblyFactory — The Blueprint

**Domain:** Assembly only. Knows the blueprint. Does not produce content.

#### What It Knows (the blueprint)

1. **Slot layout** — system_context → constraints → user_intent → tools → (LLM call) → post_processing
2. **Sub-factory registry** — which sub-factories exist, their capability declarations
3. **Routing rules** — given user input + session state, which epic sub-factory handles this request
4. **Dependency resolution** — SharedServicesFactory.safety_status must resolve before any epic sub-factory runs
5. **Force wiring** — HALT from SharedServicesFactory → skip all epic sub-factories. DOWNGRADE → swap LLM model. OVERLAY → wrap response.
6. **Session state management** — maintains and passes session state to sub-factories that require it

#### What It Does NOT Know

- What a specialty code is
- How clinical trials are searched
- What makes a URL valid
- What words are profane
- What constitutes an emergency
- How provider quality is scored
- What UMLS CPT codes look like
- What an off-topic question looks like

#### Assembly Sequence

```
1. Receive user input + session state
2. Ask SharedServicesFactory for safety_check (priority 0)
   → If force = HALT: return emergency response. STOP.
3. Route: determine which epic sub-factory handles this request
   (Routing is blueprint knowledge — intent classification rules)
4. Ask SharedServicesFactory for off_topic_detection (priority 10)
   → If force = DOWNGRADE: note LLM swap for step 7
5. Ask epic sub-factory for content PromptParts (priority 50)
   → Sub-factory reads its requires from assembly (session_state, safety_status, etc.)
   → Sub-factory returns PromptParts with content it produced
6. Collect all PromptParts, sort by slot then priority
7. Assemble final prompt from parts. Send to LLM (or commodity LLM if DOWNGRADE).
8. Receive LLM response
9. Ask SharedServicesFactory for post_processing parts (priority 70-90)
   → URL validation, CPT redaction, consent check, lead capture
   → Apply each post-processing part to response
10. If consent force = OVERLAY: wrap response with consent dialog
11. Update session state (last_state, last_specialty, off_topic_count, consent_status)
12. Return final response to UI
```

#### What Makes It Dumb

The assembly factory's intelligence is limited to:
- **Slot wiring** — which slot each part goes in (declared by the part itself)
- **Priority sorting** — what order within a slot (declared by the part itself)
- **Force propagation** — if any part says HALT, stop; if DOWNGRADE, swap LLM; if OVERLAY, wrap
- **Dependency resolution** — if a part says it depends_on safety_status, run safety first
- **Routing** — intent classification to select the epic sub-factory (this is the one piece of "intelligence" it has)

Everything else — the content of every prompt fragment, the logic of every check, the format of every response — is owned by the sub-factories.

**The routing decision is the assembly factory's one non-trivial responsibility.** It must classify user intent to pick the right epic sub-factory. This is the area most at risk of becoming god-like. The routing rules should be:
- Explicit and declarative (not a chain of if/else)
- Each epic sub-factory declares what intents it handles
- The assembly factory matches user intent to declarations
- Ambiguous intents default to FindCare (the primary epic)

---

## 5. Neighbor Awareness Matrix

This matrix shows what each sub-factory knows about its neighbors — not internal logic, just interfaces.

| Sub-Factory | Knows About SharedServices | Knows About FindCare | Knows About Narrative | Knows About EvaluateCare |
|---|---|---|---|---|
| **SharedServicesFactory** | (self) | Knows FindCare produces URLs in results | Knows Narrative produces text that may contain URLs | Knows EvaluateCare produces URLs (research sites) |
| **FindCarePromptFactory** | Knows safety_status and off_topic_count affect its behavior | (self) | Does not know about Narrative | Knows EvaluateCare may receive its selected providers |
| **NarrativePromptFactory** | Knows safety_status affects its behavior | Does not know about FindCare | (self) | Does not know about EvaluateCare |
| **EvaluateCarePromptFactory** | Knows safety_status affects its behavior | Knows FindCare provides selected_providers for quality evaluation | Does not know about Narrative | (self) |
| **PromptAssemblyFactory** | Knows SharedServices runs first and last, knows its forces | Knows FindCare handles search intents | Knows Narrative handles question intents | Knows EvaluateCare handles quality/trials intents |

**The key rule:** Each sub-factory knows its neighbors' interfaces (what they supply, what forces they create) but not their implementations (how they produce those outputs). The wing knows the engine's thrust vector but not the combustion chamber design.

---

## 6. UML Diagrams

### 6.0 Package Relationship Overview

*One diagram showing the relationship between all packages with NO internal objects. Shows dependency arrows and force flows between packages.*

### 6.1-6.5 Package Internal Diagrams

*(One per package: PromptAssemblyFactory, SharedServicesFactory, FindCarePromptFactory, NarrativePromptFactory, EvaluateCarePromptFactory)*

*Each followed by narrative explaining the internal design and flows.*

*(To be rendered after human approves the capability declarations above.)*

---

## 7. UML Sequence Diagrams

*(Not reviewed until package diagrams are complete — per human directive.)*

---

## 8. Assumptions

*(Unchanged from V3.)*

---

## 9. Known Gaps

*(Unchanged from V3.)*

---

## 10. Backlog

*(Pending — features, stories, requirements with B/T separation per v4-035. Will be created after human approves the architecture.)*
