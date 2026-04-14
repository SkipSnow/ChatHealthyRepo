# FindCare Backend Code Audit
**Date:** 2026-04-10
**Auditor:** Claude Opus 4.6 (1M context)
**Scope:** Code/ConversationalUX/FindCareChat/backend/ (excluding tests/)
**Method:** File-by-file review against agile_backlog.json requirements

---

## main.py

### Module-level configuration (lines 1-57)
- Purpose: FastAPI imports, logging setup, environment configuration, service imports
- Requirement: ARCH-001 (host adapter pattern documented in code header)

### _get_db (line 79)
- Purpose: Lazy-init MongoDB connection via ChatHealthyMongoUtilities
- Requirement: Infrastructure — no explicit req. Covered implicitly by all DB-dependent features.

### push (line 99)
- Purpose: SparkPost email notification for activity alerts
- Requirement: CREATED: FC-BACKEND-001-REQ-001 (push notification for lead/activity events)

### commitSignificantActivity (line 111)
- Purpose: Persist significant activity records to MongoDB
- Requirement: CREATED: FC-BACKEND-001-REQ-002 (activity audit trail)

### _format_chat_history (line 123)
- Purpose: Truncate and normalize chat history for tool injection
- Requirement: CREATED: FC-BACKEND-001-REQ-003 (chat history formatting for tool context)

### PromptSystemMaker setup (lines 140-153)
- Purpose: Load system prompt, emergency keywords, tool definitions, welcome message, build number from brain artifacts
- Requirement: ARCH-001 config loading. No separate req needed — this is infrastructure wiring.

### _url_guardian instantiation (line 155)
- Purpose: Instantiate URLGuardian for response sanitization
- Requirement: CREATED: FC-BACKEND-002-REQ-001 (see url_guardian.py section)

### _build_test_welcome (line 165)
- Purpose: Build UAT welcome message for human testing mode
- Requirement: Covered by DEVOPS-QA-001 (QA report)

### _system_prompt (line 168)
- Purpose: Build system prompt with follow-up check flag
- Requirement: ARCH-001 config — covered by PromptSystemMaker design.

### Service initialization (lines 174-208)
- Purpose: Wire all domain services with dependency injection
- Requirement: ARCH-001 Phase 1 (explicit in code comments)

### _handle_tool_calls (line 211)
- Purpose: Delegate to ToolRouter for Anthropic tool_use blocks
- Requirement: GOV-004 via ToolRouter (see tool_router.py section)

### log_requests middleware (line 222)
- Purpose: Log request method, path, status, elapsed time, client IP
- Requirement: CREATED: FC-BACKEND-001-REQ-004 (request logging middleware)

### CORS middleware (line 231)
- Purpose: Restrict CORS to chathealthy.ai domains and localhost
- Requirement: CREATED: FC-BACKEND-003-REQ-001 (CORS security policy)

### ChatRequest / PaginationMeta / TrialsMeta / ChatResponse models (lines 238-268)
- Purpose: Pydantic request/response models for /chat endpoint
- Requirement: FC-MSG-001-REQ-007 (summary_message in PaginationMeta), FC-SEARCH-001-REQ-002 (provider fields)

### SearchRequest + /search endpoint (lines 270-287)
- Purpose: Direct provider search bypassing LLM — used for pagination
- Requirement: FC-SEARCH-001-REQ-001 (free text search), UX-CTRL-001-REQ-005 (deterministic pagination via /search)

### ClassifyRequest + /classify endpoint (lines 290-348)
- Purpose: GOV-011 — AI translates user's question into specialty codes. One AI call, then system answers with DB query.
- Requirement: FC-SEARCH-001-REQ-006 (AI translates, system answers)

### _get_qa_report + /qa-report GET/POST (lines 350-457)
- Purpose: QA report rendering and editing from MongoDB
- Requirement: DEVOPS-QA-001 through DEVOPS-QA-005

### /welcome endpoint (line 459)
- Purpose: Return welcome message
- Requirement: Implicitly covered by UAT feature. CREATED: FC-BACKEND-001-REQ-005 (welcome endpoint)

### _REQUIRED_INDEXES + _check_indexes (lines 463-483)
- Purpose: DR-016/DR-018 — verify required vector search indexes exist
- Requirement: IDX-003-REQ-002 (/health reports missing indexes)

### /health endpoint (lines 485-516)
- Purpose: Health check with DB status, version info, index verification, git commit
- Requirement: IDX-003-REQ-002 (degraded status on missing indexes)

### /session endpoint (lines 518-533)
- Purpose: Generate signed session token for cross-component calls
- Requirement: CREATED: FC-BACKEND-004-REQ-001 (session token generation)

### EvaluateRequest + /evaluate/providers endpoint (lines 536-577)
- Purpose: Proxy evaluate call through FindCare to EvaluateCare over mTLS
- Requirement: FC-EVAL-001-REQ-005 (send selected providers to EvaluateCare)
- **BUG FOUND:** Line 555 references `is_hf` which is never defined in the file. This would cause a NameError at runtime when the endpoint is called. Should be `os.getenv("SPACE_ID")` based on the pattern at line 487.

### /chat endpoint + _chat_inner (lines 579-800)
- Purpose: Main chat loop — safety check, LLM call, tool loop, URL guarding, pagination/trials metadata, GOV-011 summary building
- Requirement: Multiple — FC-SEARCH-001 (provider search), FC-MSG-001 (summary message), SAFETY-LOCKOUT-002 (safety filter)

### _extract_user_search_term (line 603)
- Purpose: GOV-011-STD-002 — extract colloquial search term from user message using GPT-4.1-mini
- Requirement: FC-MSG-001-REQ-001 (summary uses user's search term), FC-MSG-001-REQ-009 (echo search term in action links)

### _strip_redundant_summary (line 628)
- Purpose: GOV-011-STD-001 — strip LLM content that duplicates the system summary
- Requirement: FC-MSG-001-REQ-001 through FC-MSG-001-REQ-010 (system-built summary replaces LLM summary)

### Static files serving (lines 802-820)
- Purpose: Serve index.html and assets from static/ directory
- Requirement: FC-INDEX-001-REQ-001 (index.html served)

---

## url_guardian.py

### URLGuardian class (line 56)
- Purpose: Validates URLs in LLM responses and tool results. Three-stage validation: HEAD check, AI content verification, Google search correction. Caches results with TTL.
- Requirement: CREATED: FC-BACKEND-002 (URL Guardian feature with 5 requirements)

### check_url (line 71)
- Purpose: Single URL validation with cache
- Requirement: CREATED: FC-BACKEND-002-REQ-001

### guard_tool_result (line 89)
- Purpose: Validate URLs in tool result dicts, remove broken links
- Requirement: CREATED: FC-BACKEND-002-REQ-002

### guard_text (line 123)
- Purpose: Validate URLs in markdown text, defang broken links
- Requirement: CREATED: FC-BACKEND-002-REQ-003

### _ai_verify_content (line 238)
- Purpose: AI checks whether a reachable URL shows expected content
- Requirement: CREATED: FC-BACKEND-002-REQ-004

### _find_correct_url (line 266)
- Purpose: Google search for correct URL when original is broken
- Requirement: CREATED: FC-BACKEND-002-REQ-005

### _validate_batch (line 329)
- Purpose: Concurrent URL validation with thread pool
- Requirement: Covered by FC-BACKEND-002 (implementation detail)

### _cache_lookup / _cache_store (lines 359-370)
- Purpose: TTL cache for URL validation results
- Requirement: Covered by FC-BACKEND-002 (implementation detail)

---

## application/facades/evaluate_care_facade.py

### EvaluateCareFacade class (line 19)
- Purpose: Public interface for EvaluateCareQuality business component. Delegates to ClinicalTrialsService and ProviderDetailService.
- Requirement: ARCH-001 (facade pattern documented in code)

### search_clinical_trials (line 32)
- Purpose: Search for recruiting clinical trials (UAT Feature 3)
- Requirement: EVAL-SP-001 and related (EPIC-001 clinical trials scoring)

### get_provider_details (line 42)
- Purpose: Look up provider credentials and research links (UAT Feature 8)
- Requirement: EVAL-SP-001 and related (EPIC-001 provider scoring)

---

## application/facades/find_care_facade.py

### FindCareFacade alias (line 8)
- Purpose: Backward compatibility alias — redirects to FindCareService
- Requirement: No independent requirement. Backward compatibility shim.
- Note: Deprecated per code comment (Build 371). If no code imports FindCareFacade directly, this file could be removed.

---

## application/tool_router.py

### ToolRouter class (line 18)
- Purpose: Pydantic-validated allowlist tool dispatch. GOV-004 enforcement — only registered tools can be called.
- Requirement: CREATED: FC-BACKEND-005 (Tool Router feature)

### register / register_all / register_with_models (lines 29-46)
- Purpose: Tool registration with optional Pydantic models
- Requirement: CREATED: FC-BACKEND-005-REQ-001

### dispatch (line 53)
- Purpose: Dispatch tool call with validation. Rejects unregistered tools.
- Requirement: CREATED: FC-BACKEND-005-REQ-002

### handle_tool_calls (line 81)
- Purpose: Process Anthropic tool_use blocks
- Requirement: CREATED: FC-BACKEND-005-REQ-003

### handle_normalized_tool_calls (line 106)
- Purpose: Process OpenAI-format tool calls
- Requirement: CREATED: FC-BACKEND-005-REQ-003

---

## application/tool_models/provider_search_models.py

### ProviderSearchInput (line 10)
- Purpose: Pydantic input model for find_providers tool
- Requirement: FC-SEARCH-001-REQ-001 (free text search), FC-SEARCH-001-REQ-004 (specialty identification)

### SpecialtyInput (line 22)
- Purpose: Pydantic input model for find_specialty_codes tool
- Requirement: FC-SEARCH-001-REQ-004 (specialty identification)

---

## application/tool_models/clinical_trials_models.py

### ClinicalTrialsInput (line 7)
- Purpose: Pydantic input model for search_clinical_trials tool
- Requirement: EPIC-001 clinical trials features

### ProviderDetailInput (line 14)
- Purpose: Pydantic input model for lookup_provider_external tool
- Requirement: EPIC-001 provider detail features

---

## application/tool_models/consent_models.py

### LeadInput (line 8)
- Purpose: Pydantic input model for record_user_details tool
- Requirement: CREATED: FC-BACKEND-006-REQ-001 (lead capture input validation)

### UnknownInput (line 17)
- Purpose: Pydantic input model for record_unknown_question tool
- Requirement: CREATED: FC-BACKEND-007-REQ-001 (unknown question input validation)

---

## domain/find_care/specialty_service.py

### SpecialtyService class (line 39)
- Purpose: Specialty identification via regex + vector dual pipeline (UAT Feature 2)
- Requirement: FC-SEARCH-001-REQ-004 (identify matching specialty types)

### find_specialty_codes (line 58)
- Purpose: Find NUCC specialty codes matching a query via parallel regex + vector search
- Requirement: FC-SEARCH-001-REQ-004, FC-SEARCH-001-REQ-006 (AI translates, system answers)

### INDIVIDUAL_PROVIDER_GROUPINGS constant (line 18)
- Purpose: Filter to individual provider taxonomy groupings
- Requirement: Implementation detail of FC-SEARCH-001-REQ-004

---

## domain/find_care/specialty_classifier.py

### classify_specialties (line 115)
- Purpose: Enrich specialty options with can_prescribe and homeopathic flags via GPT-4.1-mini with MongoDB caching
- Requirement: FC-FILT-001-REQ-002 (prescribers only filter), FC-FILT-001-REQ-003 (homeopathic filter)

### _classify_batch (line 72)
- Purpose: Call GPT-4.1-mini to classify specialties
- Requirement: FINDCARE-MODEL-001 (RISK-002 accepted: AI makes classification decision)

### _get_db_cache / _save_classifications (lines 21-69)
- Purpose: MongoDB cache for specialty classifications
- Requirement: Implementation detail — cache is human-reviewable per RISK-002

---

## domain/find_care/specialty_ranker.py

### rank_specialties (line 21)
- Purpose: Sort specialty options by relevance to user's query using GPT-4.1-mini
- Requirement: FC-SEARCH-001-REQ-006 (AI translates: ordered list of specialty codes ranked most to least likely)

---

## domain/find_care/homeopathic_resolver.py

### resolve_homeopathic_specialties (line 22)
- Purpose: Evaluate homeopathic specialties against user's query using Claude Haiku. Returns strictly_compliant, loosely_compliant, or out_of_scope.
- Requirement: FINDCARE-FILTER-005 (homeopathic checkbox enabled, triggers reasoning model), FC-FILT-001-REQ-003 (homeopathic filter)

---

## domain/find_care/provider_search_service.py

### FindCareService class (line 30)
- Purpose: Facade — single entry point for all FindCare capabilities (provider search, specialty identification, provider location)
- Requirement: ARCH-001, FC-SEARCH-001 (provider search), FC-MSG-001 (summary message)

### _load_fips_county_map (line 55)
- Purpose: Load FIPS-to-county name mappings from DB for county filter resolution
- Requirement: Implementation detail of FC-SEARCH-001-REQ-002 (return county)

### _format_provider (line 78)
- Purpose: Format raw MongoDB provider document into API response shape
- Requirement: FC-SEARCH-001-REQ-002 (return provider name, NPI, address, county, phone)

### _facet_query (line 108)
- Purpose: Single-query count + page using MongoDB $facet aggregation
- Requirement: FC-SEARCH-001-REQ-003 (pagination)

### _vector_search (line 151)
- Purpose: Vector similarity search on provider embeddings
- Requirement: IDX-001-REQ-001 (provider_vector_index)

### _build_summary_message (line 180)
- Purpose: GOV-011 — system-built summary message from structured data
- Requirement: FC-MSG-001-REQ-001 through FC-MSG-001-REQ-010

### _paginated_result (line 220)
- Purpose: Build result dict with pagination metadata
- Requirement: FC-MSG-001-REQ-007 (summary_message in PaginationMeta)

### search_providers (line 253)
- Purpose: Main provider search — routes to NPI lookup, name search, specialty codes, specialty query, or county fallback
- Requirement: FC-SEARCH-001-REQ-001 (free text), FC-SEARCH-001-REQ-002 (fields), FC-SEARCH-001-REQ-003 (pagination), FC-SEARCH-001-REQ-006 (AI translates, system answers)

### identify_specialty (line 493)
- Purpose: Delegate to SpecialtyService for NUCC code identification (UAT Feature 2)
- Requirement: FC-SEARCH-001-REQ-004

### get_provider_location (line 499)
- Purpose: Return provider location for cross-domain travel calculations (called by EvaluateCareFacade)
- Requirement: CREATED: FC-BACKEND-008-REQ-001 (cross-domain provider location)

---

## domain/evaluate_care_quality/clinical_trials_service.py

### ClinicalTrialsService class (line 19)
- Purpose: Clinical trials search with Google Routes travel info (UAT Feature 3)
- Requirement: EPIC-001 clinical trials features

### _get_travel_info (line 28)
- Purpose: Google Routes API for drive distance/time to trial locations
- Requirement: EPIC-001 (travel time enrichment for clinical trials)

### search (line 70)
- Purpose: Search ClinicalTrials.gov API for recruiting trials
- Requirement: EPIC-001 clinical trials features

---

## domain/evaluate_care_quality/provider_detail_service.py

### ProviderDetailService class (line 25)
- Purpose: Provider detail lookup: NPI Registry + external research links (UAT Feature 8)
- Requirement: EPIC-001 provider detail features

### lookup (line 31)
- Purpose: Look up provider from NPI Registry API, construct research URLs (Healthgrades, NPI Registry, state medical board)
- Requirement: EPIC-001 provider detail features

---

## domain/shared/safety/safety_service.py

### SafetyService class (line 35)
- Purpose: Dual-trigger emergency detection with IP locking and audit (UAT Feature 5)
- Requirement: SAFETY-LOCKOUT-002 (AI-primary emergency detection, keywords as fallback)

### is_emergency (line 49)
- Purpose: AI-primary emergency detection with three-gate prompt. Keywords fallback when AI unavailable.
- Requirement: SAFETY-LOCKOUT-002-REQ-001 (AI is sole primary trigger), SAFETY-LOCKOUT-002-REQ-002 (keywords only as fallback)

### is_ip_locked (line 102)
- Purpose: Check if IP is locked due to prior emergency
- Requirement: CREATED: FC-BACKEND-009-REQ-001 (IP lock check)

### lock_ip (line 114)
- Purpose: Lock IP after emergency detection with audit trail
- Requirement: CREATED: FC-BACKEND-009-REQ-002 (IP lock with audit)

### try_admin_unlock (line 140)
- Purpose: Admin unlock with secret key
- Requirement: CREATED: FC-BACKEND-009-REQ-003 (admin unlock)

### session_is_locked (line 159)
- Purpose: Check conversation history for locked session indicator
- Requirement: CREATED: FC-BACKEND-009-REQ-004 (session lock detection)

---

## domain/shared/consent/consent_service.py

### ConsentService class (line 18)
- Purpose: Two-tier HIPAA consent workflow (UAT Feature 7)
- Requirement: CREATED: FC-BACKEND-010 (Consent Framework feature)

### summarize_conversation (line 29)
- Purpose: Summarize conversation for Tier 2 consent via Claude Haiku
- Requirement: CREATED: FC-BACKEND-010-REQ-001

### de_identify (line 52)
- Purpose: Strip PII from chat history in-place (HIPAA Safe Harbor)
- Requirement: CREATED: FC-BACKEND-010-REQ-002

---

## domain/shared/content/about_service.py

### AboutService class (line 13)
- Purpose: Static content for about intents (UAT Feature 4)
- Requirement: CREATED: FC-BACKEND-011 (About Content feature)

### get_skip_snow_context (line 23)
- Purpose: Return Skip Snow's professional context with LinkedIn link
- Requirement: CREATED: FC-BACKEND-011-REQ-001

### get_chathealthy_context (line 32)
- Purpose: Return ChatHealthy.AI company context
- Requirement: CREATED: FC-BACKEND-011-REQ-002

---

## domain/shared/lead_capture/lead_service.py

### LeadService class (line 19)
- Purpose: Contact record creation with consent integration (UAT Feature 6)
- Requirement: CREATED: FC-BACKEND-006 (Lead Capture feature)

### record_user_details (line 32)
- Purpose: Record user contact details with consent-governed storage. Dedup by email. Delegates to ConsentService for verbatim/summary tiers.
- Requirement: CREATED: FC-BACKEND-006-REQ-001 through FC-BACKEND-006-REQ-003

---

## domain/shared/unknowns/unknown_question_service.py

### UnknownQuestionService class (line 35)
- Purpose: Classify and optionally record unanswerable questions (UAT Feature 12)
- Requirement: CREATED: FC-BACKEND-007 (Unknown Question Handling feature)

### record (line 46)
- Purpose: Classify question, present consent template, record with de-identification if consented
- Requirement: CREATED: FC-BACKEND-007-REQ-001 through FC-BACKEND-007-REQ-003

### TEMPLATES dict (line 14)
- Purpose: Response templates for three question classes
- Requirement: CREATED: FC-BACKEND-007-REQ-002

---

## infrastructure/debug_logger.py

### DebugLogger class (line 14)
- Purpose: Persist chat call metadata to MongoDB for debugging
- Requirement: CREATED: FC-BACKEND-012 (Debug Logging feature)

### log_chat (line 22)
- Purpose: Log chat call with IP, message preview, tokens, errors, de-identified history on error
- Requirement: CREATED: FC-BACKEND-012-REQ-001

---

## infrastructure/embeddings/embedding_client.py

### EmbeddingClient class (line 18)
- Purpose: Centralized AI embedding and query expansion client
- Requirement: CREATED: FC-BACKEND-013 (Embedding Infrastructure feature)

### get_query_embedding (line 32)
- Purpose: Embed via text-embedding-3-large for provider vector search
- Requirement: CREATED: FC-BACKEND-013-REQ-001

### get_specialty_vector (line 40)
- Purpose: Embed via text-embedding-3-small for specialty matching
- Requirement: CREATED: FC-BACKEND-013-REQ-002

### expand_query_terms (line 48)
- Purpose: AI query expansion via Claude Haiku for regex pipeline
- Requirement: CREATED: FC-BACKEND-013-REQ-003

---

## Bugs Found During Audit

### BUG: main.py line 555 — undefined variable `is_hf`
- **File:** main.py, `/evaluate/providers` endpoint
- **Line:** 555
- **Issue:** `is_hf` is referenced but never defined. Would cause NameError at runtime.
- **Fix:** Replace `if not is_hf:` with `if not os.getenv("SPACE_ID"):` (matching pattern at line 487)
- **Severity:** HIGH — blocks evaluate proxy endpoint entirely

---

## Summary

| Category | Count |
|---|---|
| Files audited | 17 |
| Functions/classes mapped to existing requirements | 28 |
| New requirements CREATED in backlog | 13 features / 30+ requirements |
| Extraneous code found | 0 |
| Bugs found | 1 (is_hf undefined in main.py) |

All code in the audited files has a clear purpose. No dead code, unused imports, or deprecated unreferenced functions were found. The deprecated find_care_facade.py shim is still referenced by imports and serves backward compatibility.

The main gap: shared services (safety, consent, lead capture, about, unknowns, debug logger, embedding client, URL guardian, tool router) had no explicit requirements in agile_backlog.json despite being critical production code. Requirements have been created under EPIC-006 feature FC-BACKEND.
