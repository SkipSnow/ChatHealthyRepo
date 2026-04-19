# Code Audit: Frontend, Ops, Config, and Supporting Code vs. Agile Backlog

Audited: 2026-04-10
Auditor: Claude Opus 4.6
Source of truth: brain/machine_artifacts/content/agile_backlog.json

---

## 1. Frontend TypeScript/React

### Code/ConversationalUX/FindCareChat/frontend/src/components/FindCareApp.tsx
- Purpose: Clean rebuild of FindCare React frontend. Search, filter, provider selection, evaluate handoff, postMessage bridge to parent page. Timer display during search.
- Requirement: FC-SEARCH-001 (provider search), FC-SELECT-001 (provider selection with drag/drop, max 5, garbage dismiss), FC-FILT-001 (filter apply via postMessage), FC-EVAL-001 (evaluate handoff), UX-MSG-001 (gui:render/gui:clear/gui:filter postMessage to parent), UX-MSG-002 (gui:event listener from parent), UX-CTRL-001 (pagination via loadMore cursor), SEC-HTTPS-001-REQ-003 (checkSecurityViolation on 403/426), FC-SEARCH-001-REQ-006 (GOV-011: AI classify then DB query), FC-SEARCH-001-REQ-007 (timer runs until results), FINDCARE-UX-001 (timer sent to parent control frame), FINDCARE-FILTER-004 (client-cached filter operations)
- Functions/blocks of note: `sendToParent()` implements UX-MSG-001. `checkSecurityViolation()` implements SEC-HTTPS-001. `doSearch()` implements GOV-011 two-step. `handleEvaluate()` implements FC-EVAL-001. `sendFilterToParent()` implements FC-FILT-001 filter panel rendering. `loadMore()` implements cursor pagination (UX-CTRL-001-REQ-005).

### Code/ConversationalUX/FindCareChat/frontend/src/components/ChatWindow.tsx
- Purpose: Original chat window with full message history, retry logic, timeout modal, pagination, filter apply, evaluate handoff. Uses GUIManager and SelectionManager.
- Requirement: FC-SEARCH-001, FC-SELECT-001, FC-FILT-001, FC-EVAL-001, UX-CTRL-001, UX-CTRL-003, FC-MSG-001 (system-built summary), FC-MSG-002 (action links), UX-MSG-002 (gui:event handling), SAFETY-UNLOCK-001 (session lock/unlock)
- Functions/blocks of note: `doApiCall()` handles /chat endpoint with retry and abort. EvaluateCare handoff at line 234+. Filter apply at line 111+. Pagination at line 61+. Selection panel rendered at line 727+.
- NOTE: This file appears to be the ACTIVE chat window used when App.tsx routes to FindCareApp (which does NOT import ChatWindow). App.tsx imports FindCareApp, making ChatWindow DEAD CODE. However, ChatWindow has more mature feature coverage (retry, timeout modal, filter callback registration, pagination direction). FindCareApp appears to be a newer rebuild that has NOT reached feature parity. Verdict: ChatWindow is TRANSITIONAL -- either it should be restored as the active component, or FindCareApp must absorb its missing features. Currently FindCareApp is active and ChatWindow is dead code.

### Code/ConversationalUX/FindCareChat/frontend/src/components/GUIManager.tsx
- Purpose: Orchestrates GUI controls on the parent page's control frame via postMessage. Pagination state machine, filter panel rendering, evaluate button, prescriber/homeopathic toggle (client-side, no server round-trip).
- Requirement: UX-MSG-001 (gui:render, gui:clear, gui:filter postMessage), UX-MSG-002 (gui:event listener), UX-CTRL-001 (pagination controls: Back/Forward with 3D button style, disabled states with tooltips), UX-CTRL-001-REQ-007 (3D raised button style), UX-CTRL-002 (responsive rows: PC=20, mobile=5), UX-CTRL-003 (Evaluate These Providers button), FC-FILT-001-REQ-001 through REQ-009 (filter panel rendering with specialty checkboxes), FC-FILT-001-REQ-004 (three counts), FC-FILT-001-REQ-005 (toggle all), FC-FILT-001-REQ-006 (horizontal grid header), FINDCARE-UX-003 (prescribers checkbox), FINDCARE-UX-004/FINDCARE-FILTER-005 (homeopathic checkbox, enabled), FINDCARE-FILTER-001 (three labeled numbers), FINDCARE-FILTER-002 (switch defaults and re-render), FINDCARE-FILTER-004 (client-cached filter)
- NOTE: Only imported by ChatWindow.tsx (dead code). NOT used by FindCareApp.tsx. FindCareApp has its own inline sendFilterToParent() which duplicates some of this logic but is less complete. This makes GUIManager effectively DEAD CODE in the current active path.

### Code/ConversationalUX/FindCareChat/frontend/src/components/MessageBubble.tsx
- Purpose: Renders chat messages with Markdown, GFM tables, sanitized HTML, action links (#action:filter, #action:next-page).
- Requirement: FC-MSG-001 (system-built summary rendering), FC-MSG-002 (action links for filter highlight and next page)
- NOTE: Only imported by ChatWindow.tsx (dead code). FindCareApp.tsx does not use MessageBubble -- it renders inline HTML. This makes MessageBubble effectively DEAD CODE in the current active path.

### Code/ConversationalUX/FindCareChat/frontend/src/App.tsx
- Purpose: React app entry point. Routes to FindCareApp.
- Requirement: Structural -- no specific requirement. Part of React 18 / Vite 5 stack (SDT-FC-001).

### Code/ConversationalUX/FindCareChat/frontend/src/main.tsx
- Purpose: React DOM entry point. Mounts App into #root with StrictMode.
- Requirement: Structural -- no specific requirement. Part of React 18 / Vite 5 stack.

### Code/ConversationalUX/FindCareChat/frontend/src/vite-env.d.ts
- Purpose: Vite type declarations reference.
- Requirement: Structural -- build tooling.

### Code/ConversationalUX/FindCareChat/frontend/vite.config.ts
- Purpose: Vite build configuration. React plugin, output to dist/.
- Requirement: NEEDS REQUIREMENT: Vite build configuration is implicit in the stack but not explicitly tracked. Part of "React 18 / Vite 5" realized_by on many requirements.

---

## 2. Shared UX Components

### Code/Shared/ux/components/ProviderCard.tsx
- Purpose: Single provider display card with action buttons (select arrow, dismiss garbage, deselect X). Supports available and selected modes, compact display, drag-and-drop, filtered-out highlighting.
- Requirement: FC-SELECT-001-REQ-002 (drag start), FC-SELECT-001-REQ-003 (arrow button to select), FC-SELECT-001-REQ-004 (delete icon on selected), FC-SELECT-001-REQ-008 (garbage icon dismiss), FC-SELECT-001-REQ-012 (filtered-out with color change and tooltip), FC-SELECT-001-REQ-016 (compact mode: name, specialty, NPI only with tooltip for details), FC-SEARCH-001-REQ-005 (specialty shown on second row)

### Code/Shared/ux/components/SelectionManager.tsx
- Purpose: Split view for provider selection. Available providers (top, scrollable) + Selected providers (bottom, max 5). Drop zone for drag-and-drop. Garbage count badge. Filtered-out dialog (keep/remove).
- Requirement: FC-SELECT-001-REQ-001 (split view: top available, bottom selected max 5), FC-SELECT-001-REQ-002 (drag and drop), FC-SELECT-001-REQ-005 (max 5 enforcement), FC-SELECT-001-REQ-008 (garbage icon with count badge), FC-SELECT-001-REQ-012 (filtered-out selected provider highlighted), FC-SELECT-001-REQ-013 (click filtered-out gives keep/delete choice)
- NOTE: Only imported by ChatWindow.tsx (dead code path). FindCareApp.tsx has its own inline selection UI that replicates some but not all of this functionality (it uses ProviderCard directly and has its own drop zone). SelectionManager is MORE COMPLETE than FindCareApp's inline version.

### Code/Shared/ux/hooks/useSelectionState.ts
- Purpose: Selection state reducer. Available/selected/garbage with max-5 limit. Flush garbage on new question. Keep/remove filtered. NPI normalization to string.
- Requirement: FC-SELECT-001-REQ-005 (max 5), FC-SELECT-001-REQ-009 (dismissed stay gone until new question), FC-SELECT-001-REQ-010 (flush garbage on new question), FC-SELECT-001-REQ-014 (persist across pagination), FC-SELECT-001-REQ-015 (garbage is JS array)
- Functions/blocks of note: `selectionReducer` handles SET_AVAILABLE, SELECT, DESELECT, DISMISS, FLUSH_GARBAGE, KEEP_FILTERED, REMOVE_FILTERED.

### Code/Shared/ux/types/provider.ts
- Purpose: Shared TypeScript types for Provider, FilterOption, SelectionState, SelectionAction, FilterState.
- Requirement: FC-SEARCH-001-REQ-002 (Provider type includes npi, name, address, county, phone), FC-SELECT-001 (SelectionState with available/selected/garbage/maxSelected), FC-FILT-001 (FilterOption with can_prescribe, homeopathic flags)

---

## 3. Website HTML with JavaScript

### Website/index.html
- Purpose: Main parent page. Frame layout (header 6%, footer 4%, side panels, center with chat iframe + control frame). postMessage handlers for gui:render, gui:clear, gui:filter, gui:timer, gui:evaluate-result, gui:session-display. Iframe auto-detection (localhost vs deployed). Retry logic for iframe. Legal/leaf panel (right side). Hamburger menu. Mobile responsive.
- Requirement: UX-FRAME-001 (percentage-based frame: header 6%, footer 4%, chat 83%, control 7%), UX-FRAME-002 (left/right panels), UX-MSG-001 (postMessage handlers for gui:render, gui:clear, gui:eval-render), UX-MSG-001-REQ-004 (origin validation), UX-MSG-002 (gui:event relay to iframe), UX-IFRAME-001 (auto-detect localhost/deployed for iframe src), UX-IFRAME-002 (retry on backend startup), UX-LEAF-001 (right panel management), UX-MOB-001 (hamburger menu at <=600px), UX-MOB-002 (back to home link), FC-INDEX-001 (served on HTTP :80 with 301 redirect -- implemented in Caddyfile), FINDCARE-UX-001 (timer display in control frame), FINDCARE-UX-002 (filter panel scroll bar, max 15 items), FINDCARE-FILTER-001 (three labeled numbers in filter header)
- NOTE: UX-GEN-001 requires this file to be generated from design.json by generate_index_page.py. The file does NOT have a "GENERATED FILE" comment header (UX-GEN-001-REQ-002 not yet implemented). generate_index_page.py does not exist yet.

### Website/architecture.html
- Purpose: Architecture documentation leaf page.
- Requirement: UX-LEAF-001 (leaf page loaded into right panel), UX-MOB-001 (hamburger menu), UX-MOB-002 (back to home link)

### Website/security-architecture.html
- Purpose: Security architecture documentation. Generated by generate_security_page.py.
- Requirement: UX-LEAF-001, UX-MOB-001, UX-MOB-002. Generated content maps to EPIC-4 SEC-* requirements display.

### Website/roadmap.html
- Purpose: Roadmap with milestone timeline. Milestones section generated by generate_roadmap_page.py.
- Requirement: UX-LEAF-001, UX-MOB-001, UX-MOB-002

### Website/products.html
- Purpose: Products and services page.
- Requirement: UX-LEAF-001, UX-MOB-001, UX-MOB-002

### Website/privacy.html
- Purpose: Privacy policy.
- Requirement: UX-LEAF-001, UX-MOB-001, UX-MOB-002. Legal requirement (implied, no explicit req_id).

### Website/terms.html
- Purpose: Terms of service.
- Requirement: UX-LEAF-001, UX-MOB-001, UX-MOB-002. Legal requirement (implied, no explicit req_id).

### Website/chat-app-design.html
- Purpose: Chat app design documentation leaf page.
- Requirement: UX-LEAF-001. NEEDS REQUIREMENT: No specific backlog item for design documentation pages.

### Website/embedding-design.html
- Purpose: Embedding design documentation leaf page.
- Requirement: UX-LEAF-001. NEEDS REQUIREMENT: No specific backlog item for this design doc.

### Website/load-perf-report.html
- Purpose: Load performance report page.
- Requirement: NEEDS REQUIREMENT: No specific backlog item for performance reporting page.

### Website/ops-manager-design.html
- Purpose: Ops manager design documentation page.
- Requirement: NEEDS REQUIREMENT: No specific backlog item for this design doc.

### Website/provider-data-load.html
- Purpose: Provider data load documentation page.
- Requirement: NEEDS REQUIREMENT: No specific backlog item for this data load doc.

---

## 4. GitHub Actions Workflows

### .github/workflows/deploy-findcare-backend.yml
- Purpose: Deploy FindCare backend + frontend to HuggingFace on push to dev/qa/main.
- Requirement: SDT-FC-001 (FindCare HF Space). NEEDS REQUIREMENT: No explicit CI/CD workflow requirements in backlog beyond SDT-FC-001.

### .github/workflows/deploy-findcare-website-dev.yml
- Purpose: Deploy Website/ to Cloudflare Pages (dev environment).
- Requirement: SDT-PUBLIC-SITE (Cloudflare Pages). DEVOPS-QA-001 through DEVOPS-QA-005 (QA report generation as part of deploy).

### .github/workflows/deploy-findcare-website-qa.yml
- Purpose: Deploy Website/ to Cloudflare Pages (QA environment).
- Requirement: SDT-PUBLIC-SITE. DEVOPS-QA-001 through DEVOPS-QA-005.

### .github/workflows/deploy-evaluatecare-backend.yml
- Purpose: Deploy EvaluateCare to HuggingFace on push to dev/qa/main.
- Requirement: SDT-EC-001 (EvaluateCare HF Space).

### .github/workflows/deploy-shared-services.yml
- Purpose: Deploy Shared Services to HuggingFace on push to dev/qa/main.
- Requirement: NEEDS REQUIREMENT: No explicit SDT story for Shared Services deployment workflow. SDT-BRAIN is closest but does not cover deploy CI/CD.

### .github/workflows/deploy-pipelines.yml
- Purpose: Deploy data pipelines to Azure Functions on push to dev/qa/main.
- Requirement: SDT-PIPE-001 (Pipeline Azure Functions).

### .github/workflows/promote-build.yml
- Purpose: Promote build between environments (dev->qa, qa->prod). Manual trigger.
- Requirement: NEEDS REQUIREMENT: Build promotion is operational but no explicit backlog requirement. Maps conceptually to PIPE-LC (pipeline lifecycle).

### .github/workflows/promote-data.yml
- Purpose: Promote provider data between environments. Manual trigger.
- Requirement: NEEDS REQUIREMENT: Data promotion is operational but no explicit backlog requirement. Maps conceptually to PIPE-LC.

### .github/workflows/test.yml
- Purpose: Run tests on push to main and PRs to main.
- Requirement: NEEDS REQUIREMENT: No explicit CI test workflow requirement. Implied by all pytest_ids in backlog requirements.

---

## 5. Ops Scripts

### Code/Shared/ops/bump_build.py
- Purpose: Increment system-wide build counter in MongoDB per environment.
- Requirement: NEEDS REQUIREMENT: Build numbering is operational infrastructure. Referenced by deploy workflows.

### Code/Shared/ops/promote_build.py
- Purpose: Promote build number between environments. Copies number (no increment) and merges branch.
- Requirement: NEEDS REQUIREMENT: Maps to promote-build.yml workflow.

### Code/Shared/ops/promote_data.py
- Purpose: Promote provider data between environments on frontend cluster.
- Requirement: NEEDS REQUIREMENT: Maps to promote-data.yml workflow.

### Code/Shared/ops/hf_space_create.py
- Purpose: Create HuggingFace Space with environment-specific config.
- Requirement: SDT-FC-001, SDT-EC-001 (Space creation for services).

### Code/Shared/ops/hf_space_delete.py
- Purpose: Delete HuggingFace Space with prod safety check.
- Requirement: NEEDS REQUIREMENT: Operational tooling, no explicit backlog item.

### Code/Shared/ops/hf_space_restart.py
- Purpose: Restart HuggingFace Space (normal or factory).
- Requirement: NEEDS REQUIREMENT: Operational tooling.

### Code/Shared/ops/hf_space_status.py
- Purpose: Check HuggingFace Space status.
- Requirement: NEEDS REQUIREMENT: Operational tooling.

### Code/Shared/ops/atlas_cluster_toggle.py
- Purpose: Pause/resume MongoDB Atlas cluster.
- Requirement: NEEDS REQUIREMENT: Cost management operational tooling.

### Code/Shared/ops/framework_version.py
- Purpose: Read/set framework version in MongoDB admin database.
- Requirement: NEEDS REQUIREMENT: Version tracking operational tooling.

### Code/Shared/ops/generate_roadmap_page.py
- Purpose: Generate milestone timeline section of Website/roadmap.html from brain JSON.
- Requirement: UX-GEN-001 (index.html generator concept). NEEDS REQUIREMENT: Roadmap page generation is not explicitly in backlog. Related to UX-GEN-001 pattern.

### Code/Shared/ops/generate_security_page.py
- Purpose: Generate Website/security-architecture.html from brain JSON sources.
- Requirement: NEEDS REQUIREMENT: Security page generation not explicitly in backlog. Same pattern as UX-GEN-001.

### Code/Shared/ops/local_admin_server.py
- Purpose: Local HTTPS server for admin content on port 443.
- Requirement: SDT-LOCAL-004 (admin site on port 443 with self-signed cert).

### Code/Shared/ops/manifest_generator.py
- Purpose: Produces complete project manifest: files, hashes, entity types, capabilities.
- Requirement: NEEDS REQUIREMENT: Manifest generation is operational infrastructure.

### Code/Shared/ops/rebuild_manifest.py
- Purpose: Walk repo and rebuild manifest JSON with file hashes.
- Requirement: NEEDS REQUIREMENT: Same as manifest_generator.py -- operational infrastructure.

### Code/Shared/ops/sync_to_mongo.py
- Purpose: Push all project files from local disk to admin.project_files in MongoDB.
- Requirement: NEEDS REQUIREMENT: Brain sync operational infrastructure.

### Code/Shared/ops/uat_report.py
- Purpose: Generate UAT welcome report from brain/uat config.
- Requirement: DEVOPS-QA-001 (QA report generation).

### Code/Shared/ops/conversation_log_hook.py
- Purpose: Claude Code hook that appends utterances to conversation_log.json.
- Requirement: NEEDS REQUIREMENT: Governance logging (implied by brain operating model).

### Code/Shared/ops/unattended_monitor.py
- Purpose: Semaphore-based unattended job monitor for brain assignments.
- Requirement: NEEDS REQUIREMENT: Brain automation infrastructure.

### Code/Shared/ops/epic_planning_runner.py
- Purpose: Tree-based epic planning with JSON Schema enforcement. GPT proposes, Claude accepts/rejects.
- Requirement: NEEDS REQUIREMENT: Brain planning automation. Used to build agile_backlog.json.

### Code/Shared/ops/dev_pipeline.py
- Purpose: Dev pipeline orchestration -- runs all pre-commit/pre-deploy steps.
- Requirement: NEEDS REQUIREMENT: Dev workflow automation.

### Code/Shared/ops/brain_snapshot.py
- Purpose: Produces manifest-level snapshot of brain state from local disk.
- Requirement: NEEDS REQUIREMENT: Brain state management.

### Code/Shared/ops/brain_biz_arch_collab.py
- Purpose: Claude + GPT overnight collaboration for business architecture.
- Requirement: NEEDS REQUIREMENT: Brain collaboration tooling. May be one-time use.

### Code/Shared/ops/brain_biz_arch_diagrams.py
- Purpose: Overnight: rewrite biz arch + generate diagrams via Claude/GPT collaboration.
- Requirement: NEEDS REQUIREMENT: Brain collaboration tooling. May be one-time use.

### Code/Shared/ops/brain_refactor_collab.py
- Purpose: Claude + GPT iterative design collaboration for ARCH-001.
- Requirement: NEEDS REQUIREMENT: Brain collaboration tooling. May be one-time use.

### Code/Shared/ops/design_review_collab.py
- Purpose: GPT design review collaboration for BIZOPS-CHATLOG-001.
- Requirement: NEEDS REQUIREMENT: Brain collaboration tooling. May be one-time use.

### Code/Shared/ops/ask_gpt_crosswalk.py
- Purpose: One-shot GPT consultation on crosswalk patent + RAG structure.
- Requirement: EXTRANEOUS: One-time consultation script. No ongoing operational purpose.

### Code/Shared/ops/ask_gpt_umls_review.py
- Purpose: One-shot GPT consultation on UMLS license review.
- Requirement: UMLS-NLM-001, UMLS-ATTR-001 (UMLS license compliance). EXTRANEOUS as ongoing tool: one-time consultation.

### Code/Shared/ops/build_code_review_pdf.py
- Purpose: Generate code review business document from brain JSON.
- Requirement: NEEDS REQUIREMENT: Business document generation tooling.

### Code/Shared/ops/build_repo_json.py
- Purpose: Export full repository as single JSON for web-based LLMs.
- Requirement: NEEDS REQUIREMENT: Brain communication tooling.

### Code/Shared/ops/gen_evaluate_care_v3.py
- Purpose: Generate Evaluate Care diagram using matplotlib.
- Requirement: EXTRANEOUS: One-time diagram generation script.

### Code/Shared/ops/gen_investor_docx.py
- Purpose: Generate investor document from business_plan.json.
- Requirement: NEEDS REQUIREMENT: Business document generation.

### Code/Shared/ops/gen_investor_docx_v3.py
- Purpose: Generate investor document v3 with SGML markup resolution.
- Requirement: NEEDS REQUIREMENT: Business document generation (supersedes gen_investor_docx.py).

### Code/Shared/ops/gen_investor_pdf_v2.py
- Purpose: Generate investor PDF with inline diagrams.
- Requirement: NEEDS REQUIREMENT: Business document generation.

### Code/Shared/ops/gen_moat_v4.py
- Purpose: Generate competitive moat diagram using matplotlib.
- Requirement: EXTRANEOUS: One-time diagram generation script.

### Code/Shared/ops/test_brain_questions.py
- Purpose: Test GPT brain question answering with system prompt.
- Requirement: EXTRANEOUS: One-time test/verification script.

### Code/Shared/ops/update_brain_descriptions.py
- Purpose: Update brain JSON file descriptions.
- Requirement: EXTRANEOUS: One-time data migration script.

### Code/Shared/ops/update_manifest_capabilities.py
- Purpose: Update manifest with file capabilities mapping.
- Requirement: EXTRANEOUS: One-time data migration script.

### Code/Shared/ops/tools/scan_http.py
- Purpose: Scan files for insecure HTTP URLs. SEC-HTTPS-001-REQ-004 enforcement.
- Requirement: SEC-HTTPS-001-REQ-004 (no HTTP URLs in production code), FC-INDEX-001 (only 4 named hosts allowed for index.html redirect).

### Code/Shared/ops/tools/pre_deploy_rule_check.py
- Purpose: Read and enforce all development/operating rules before deployment.
- Requirement: NEEDS REQUIREMENT: Governance enforcement tooling. Implied by v4-017 design rule.

### Code/Shared/ops/tools/kill_zombies.py
- Purpose: Kill zombie processes on service ports before starting local servers.
- Requirement: SDT-LOCAL-003-REQ-003 through REQ-005 (kill zombies on all service ports). DR-009 reference.

### Code/Shared/ops/tools/chathealthy_devops_boot.py
- Purpose: Governance entry point for Claude Code. Four modes: boot, prompt, tool_call, prompt_result.
- Requirement: NEEDS REQUIREMENT: Claude Code governance hook infrastructure.

### Code/Shared/ops/tools/bash_rule_guard.py
- Purpose: Claude Code PreToolUse guard. Allowlist approach for Bash/Edit/Write tool calls.
- Requirement: NEEDS REQUIREMENT: Claude Code governance hook infrastructure.

### Code/Shared/ops/tools/create_hf_space.py
- Purpose: Create HuggingFace Space (DR-026: only way to create).
- Requirement: SDT-FC-001, SDT-EC-001. NEEDS REQUIREMENT: Explicit DR-026 enforcement requirement.

### Code/Shared/ops/tools/disk_cleanup.py
- Purpose: Uninstall unused programs, clean PATH and registry.
- Requirement: EXTRANEOUS: One-time system maintenance script.

### Code/Shared/ops/tools/cleanup_path_registry.py
- Purpose: PATH + Registry cleanup on Windows.
- Requirement: EXTRANEOUS: One-time system maintenance script.

### Code/Shared/ops/tools/gen_ops_design_docx.py
- Purpose: Generate ops manager design Word document.
- Requirement: EXTRANEOUS: One-time document generation script.

### Code/Shared/ops/tools/add_display_bug.py
- Purpose: One-shot script to add BUG-UX-005 to bugs.json.
- Requirement: EXTRANEOUS: One-time bug insertion script.

### Code/Shared/ops/tools/add_prompt_override_bug.py
- Purpose: One-shot script to add BUG-GOV-002 to bugs.json.
- Requirement: EXTRANEOUS: One-time bug insertion script.

### Code/Shared/ops/tools/add_quality_bug.py
- Purpose: One-shot script to add BUG-PIPE-010 to bugs.json.
- Requirement: EXTRANEOUS: One-time bug insertion script.

### Code/Shared/ops/tools/add_risk_acceptance_to_bugs.py
- Purpose: One-shot script to add risk_acceptance_id=null to all bugs.
- Requirement: EXTRANEOUS: One-time schema migration script.

### Code/Shared/ops/tools/add_showstopper_bug.py
- Purpose: One-shot script to add BUG-DATA-001 to bugs.json.
- Requirement: EXTRANEOUS: One-time bug insertion script.

---

## 6. Shared Python

### Code/Shared/llm_client.py
- Purpose: Unified LLM client. Routes by model name to Anthropic or OpenAI SDK. No vendor lock-in.
- Requirement: FINDCARE-MODEL-001 (two-model strategy: commodity GPT-4.1-mini + reasoning Claude Sonnet). NEEDS REQUIREMENT: Explicit requirement for unified LLM client abstraction.

### Code/Shared/session_token.py
- Purpose: Session token signed with service private key, verified with public cert. Zero-trust component-to-component auth.
- Requirement: SEC-MTLS-001 (FindCare presents client certificate), FC-EVAL-001-REQ-005 (signed session token with handoff). NEEDS REQUIREMENT: Explicit session token requirement.

### Code/Shared/cost_guard.py
- Purpose: Token budget enforcement for Brain Loop. Tracks API call costs.
- Requirement: NEEDS REQUIREMENT: Budget governance. Implied by operating model.

### Code/Shared/brain_loop.py
- Purpose: Claude-GPT autonomous review loop (Binary Operating Model).
- Requirement: NEEDS REQUIREMENT: Brain operating model automation.

### Code/Shared/brain_runner.py
- Purpose: Drives Claude-GPT loop via OpenAI API. Human approves High+ risk gates.
- Requirement: NEEDS REQUIREMENT: Brain operating model automation.

### Code/Shared/machine_brain.py
- Purpose: Persistent architectural memory. Voyage AI embeddings, Atlas Vector Search.
- Requirement: NEEDS REQUIREMENT: Brain memory infrastructure.

### Code/Shared/prompt_system_maker.py
- Purpose: Builds runtime configuration from brain artifacts (system prompts, tool definitions, emergency keywords).
- Requirement: BRAIN-PROMPTS-001 (prompt schema), BRAIN-PROMPTS-002 (prompt migration). NEEDS REQUIREMENT: Explicit requirement for prompt system maker.

### Code/Shared/brain_auth.py
- Purpose: Brain API authentication. Bearer token to agent identity + scopes.
- Requirement: NEEDS REQUIREMENT: Brain API security infrastructure.

### Code/Shared/ChatHealthyMongoUtilities.py
- Purpose: MongoDB connection manager shared across all services.
- Requirement: NEEDS REQUIREMENT: Database infrastructure. Implied by all MongoDB Atlas realized_by entries.

### Code/Shared/agent_framework/__init__.py
- Purpose: Package init for agent framework base classes.
- Requirement: NEEDS REQUIREMENT: Agent framework infrastructure. Pre-alpha for future agentic capabilities.

### Code/Shared/agent_framework/base_agent.py
- Purpose: BaseAgent abstract class. Has tools, receives events, decides, acts.
- Requirement: NEEDS REQUIREMENT: Agent framework infrastructure.

### Code/Shared/agent_framework/base_tool.py
- Purpose: BaseTool interface. Tools execute; agent decides when.
- Requirement: NEEDS REQUIREMENT: Agent framework infrastructure.

### Code/Shared/agent_framework/tool_registry.py
- Purpose: Allowlist of tools an agent can use (GOV-004 pattern).
- Requirement: NEEDS REQUIREMENT: Agent framework infrastructure.

---

## 7. EvaluateCare

### Code/evaluate_care/app.py
- Purpose: EvaluateCare FastAPI service on port 8001. Separate from FindCare (GOV-005).
- Requirement: SDT-LOCAL-002-REQ-001 (port 8001), SDT-LOCAL-002-REQ-002 (CORS), SDT-LOCAL-002-REQ-003 (/health endpoint), SDT-EC-001 (EvaluateCare service).

### Code/evaluate_care/scoring_engine.py
- Purpose: Composite scoring engine. Deterministic: same input always yields same output.
- Requirement: EVAL-REQ-001 through EVAL-REQ-014 (deterministic scoring, normalization, aggregation, missing data handling, trace output), EVAL-SP-002 (core scoring logic).

### Code/evaluate_care/models.py
- Purpose: Pydantic models for all input/output schemas (provider scoring, clinical trials, provenance, confidence, cache).
- Requirement: EVAL-SP-001-REQ-001 through REQ-015 (input model, weights, validation, output model with composite_score, subscores, trace).

### Code/evaluate_care/normalization.py
- Purpose: Centralized normalization utilities (min-max, etc.).
- Requirement: EVAL-REQ-003 (normalize to consistent scale), EVAL-NORM-* requirements.

### Code/evaluate_care/weights.py
- Purpose: Default weight configurations for provider and clinical trial measures.
- Requirement: EVAL-WEIGHTS-* requirements, EVAL-REQ-004 (fixed weighting logic).

### Code/evaluate_care/cache.py
- Purpose: Thread-safe LRU cache for scored results.
- Requirement: EVAL-CACHE-REQ-001 through REQ-020 (cache requirements).

### Code/evaluate_care/confidence.py
- Purpose: Confidence indicator computation from scored measure traces.
- Requirement: EVAL-CONF-REQ-001 through REQ-015 (confidence computation).

### Code/evaluate_care/explainability.py
- Purpose: Human-readable score explanations.
- Requirement: EVALEXPLAIN-* requirements (explainability requirements).

### Code/evaluate_care/failsafe.py
- Purpose: Graceful degradation when data is missing or invalid.
- Requirement: EVALFAILSAFE-* requirements (failsafe requirements).

### Code/evaluate_care/provenance.py
- Purpose: Data lineage tracking (input hash, source metadata).
- Requirement: EVAL-DATA-PROV-* requirements (data provenance).

### Code/evaluate_care/measures/base.py
- Purpose: Base class for all measure implementations.
- Requirement: Structural base for all EVAL-P-MEASURE-* and EVAL-CT-MEASURE-* requirements.

### Code/evaluate_care/measures/board_certification.py
- Purpose: EVAL-P-MEASURE-1 -- Board Certification measure.
- Requirement: EVAL-P-M2-* (measure requirements for board certification).

### Code/evaluate_care/measures/years_in_practice.py
- Purpose: EVAL-P-MEASURE-2 -- Years in Practice measure.
- Requirement: EVAL-P-M2-S2-* (years in practice requirements).

### Code/evaluate_care/measures/specialty_match.py
- Purpose: EVAL-P-MEASURE-3 -- Specialty Match measure.
- Requirement: EVAL-P-M2-S3-* (specialty match requirements).

### Code/evaluate_care/measures/patient_ratings.py
- Purpose: EVAL-P-MEASURE-4 -- Patient Ratings measure.
- Requirement: EVAL-P3-* (patient ratings requirements).

### Code/evaluate_care/measures/new_patient_acceptance.py
- Purpose: EVAL-P-MEASURE-5 -- New Patient Acceptance measure.
- Requirement: EVAL-P5-* (new patient acceptance requirements).

### Code/evaluate_care/measures/prescription_behavior.py
- Purpose: EVAL-P-RX -- Prescription Behavior measure.
- Requirement: EVAL-P-RX-TEST-* (prescription behavior requirements).

### Code/evaluate_care/measures/trial_phase.py
- Purpose: EVAL-CT-MEASURE-1 -- Clinical Trial Phase measure.
- Requirement: EVAL-CT-M1-S2-* (trial phase requirements).

### Code/evaluate_care/measures/trial_recruitment.py
- Purpose: EVAL-CT-MEASURE-2 -- Clinical Trial Recruitment Status measure.
- Requirement: EVALCT-M2-* (trial recruitment requirements).

### Code/evaluate_care/measures/trial_condition_relevance.py
- Purpose: EVAL-CT-MEASURE-3 -- Clinical Trial Condition Relevance measure.
- Requirement: EVAL-CT-M3-S3-*, EVAL-CT-M3-S4-* (condition relevance requirements).

### Code/evaluate_care/measures/trial_sponsor.py
- Purpose: EVAL-CT-MEASURE-4 -- Clinical Trial Sponsor Credibility measure.
- Requirement: EVALCT-M4-* (sponsor credibility requirements).

### Code/evaluate_care/measures/trial_proximity.py
- Purpose: EVAL-CT-MEASURE-5 -- Clinical Trial Proximity measure.
- Requirement: EVALCT-M5-* (proximity requirements).

### Code/evaluate_care/measures/trial_recency.py
- Purpose: EVAL-CT-MEASURE-6 -- Clinical Trial Recency measure.
- Requirement: EVALCT-M6-* (recency requirements).

### Code/evaluate_care/measures/trial_eligibility.py
- Purpose: EVAL-CT-MEASURE-7 -- Clinical Trial Eligibility Match measure.
- Requirement: EVALCT-M7-* (eligibility requirements).

### Code/evaluate_care/__init__.py
- Purpose: Package init exposing ScoringEngine.
- Requirement: Structural.

---

## 8. Shared Services

### Code/shared_services/app.py
- Purpose: Shared Services FastAPI on port 8002. Cross-cutting infrastructure: SafetyService, ConsentService, SecretManager, etc. mTLS required for all callers.
- Requirement: SEC-MTLS-002 (FindCare presents client cert when calling Shared Services), SEC-SM-001 through SEC-SM-004 (SecretManager requirements), SDT-BRAIN (agentic infrastructure). NEEDS REQUIREMENT: Explicit Shared Services service definition requirement.

---

## 9. Config Files That Execute

### Code/Shared/ops/Caddyfile
- Purpose: Local dev reverse proxy. HTTP :80 (redirect index only, 426 for everything else), HTTPS :443 (website + API proxy), :3000 (React via Vite), :8080 (FindCare), :8081 (EvaluateCare mTLS), :8082 (Shared Services mTLS).
- Requirement: FC-INDEX-001-REQ-001 (HTTP :80 / returns 301), FC-INDEX-001-REQ-002 (HTTP /index.html returns 301), FC-INDEX-001-REQ-003 (all other HTTP returns 426), SDT-LOCAL-001-REQ-001 (port 80 website), SDT-LOCAL-004 (HTTPS :443 with self-signed cert), SEC-MTLS-001 (EvaluateCare mTLS on :8081), SEC-MTLS-002 (Shared Services mTLS on :8082), SEC-HTTPS-001 (HTTPS enforcement)

### start_local.bat
- Purpose: One-click startup for local dev environment. Kills zombies, TypeScript compile check, starts Caddy, Vite, FindCare, EvaluateCare. Verifies all services.
- Requirement: SDT-LOCAL-003-REQ-001 (launches all services), SDT-LOCAL-003-REQ-002 (admin for port 80 -- documented in comments), SDT-LOCAL-003-REQ-005 (kills zombies on ports 80,443,5173,8000,8001), SDT-LOCAL-003-REQ-006 (logs to %TEMP%/chathealthy_*.log), SDT-LOCAL-003-REQ-007 (reports log file on failure), SDT-LOCAL-003-REQ-008 (verifies EvaluateCare on :8001), FINDCARE-UX-006 (single command boots everything), PIPE-DQ-004-REQ-021 (single start_local.bat boots all services)
- NOTE: SDT-LOCAL-003-REQ-003 (kill orphaned python.exe) and SDT-LOCAL-003-REQ-004 (kill orphaned node.exe) are implemented via port-based killing, not process-name killing. The current implementation kills by port, which may miss orphans on non-standard ports. Port 8002 (Shared Services) is not started by this script despite being in kill list. Shared Services is not yet started locally.

---

## 10. Brain Scripts

### brain/machine_artifacts/code/add_cost_reduction.py
- Purpose: One-time script to add cost reduction feature to Sprint 1 plan tree.
- Requirement: EXTRANEOUS: One-time plan tree modification script. References .iteration_cache/plan_tree.json (legacy planning artifact).

### brain/machine_artifacts/code/add_maintenance_features.py
- Purpose: One-time script to add maintenance features to plan tree.
- Requirement: EXTRANEOUS: One-time plan tree modification.

### brain/machine_artifacts/code/cleanup_epics.py
- Purpose: One-time script to clean up epics per human directives.
- Requirement: EXTRANEOUS: One-time plan tree modification.

### brain/machine_artifacts/code/generate_arch001_pdf.py
- Purpose: Generate ARCH-001 Refactor Design PDF.
- Requirement: EXTRANEOUS: One-time document generation.

### brain/machine_artifacts/code/generate_audit_report_doc.py
- Purpose: Convert audit HTML to Word document.
- Requirement: EXTRANEOUS: One-time document generation.

### brain/machine_artifacts/code/generate_epic_plan_artifacts.py
- Purpose: Produce Word doc and JSON from plan_tree.json.
- Requirement: EXTRANEOUS: One-time document generation.

### brain/machine_artifacts/code/generate_epic_planning_doc.py
- Purpose: Generate epic planning prompt Word doc.
- Requirement: EXTRANEOUS: One-time document generation.

### brain/machine_artifacts/code/update_rx_features.py
- Purpose: Update prescription behavior features with two-signal design.
- Requirement: EXTRANEOUS: One-time plan tree modification.

### brain/machine_artifacts/code/update_triage_and_rejected.py
- Purpose: Move dropped measures to triage or rejected candidates list.
- Requirement: EXTRANEOUS: One-time plan tree modification.

---

## 11. Archive

### archive/V0_1/legacy_machine_docs/machine_brain_seed.py
- Purpose: Bootstrap Machine Brain with initial 11 ADR decisions. Run once per environment.
- Requirement: EXTRANEOUS: Legacy V0.1 seed script. Superseded by current brain architecture.

### archive/V0_1/legacy_machine_docs/machine_brain_seed_gpt.py
- Purpose: Seed GPT's Machine Brain records (MB-0000 through MB-0099). Run once per environment.
- Requirement: EXTRANEOUS: Legacy V0.1 seed script. Superseded.

### archive/V0_1/legacy_machine_docs/seed_findcare_requirements.py
- Purpose: Seed FindCare product requirements into Machine Brain by reverse-engineering app.py.
- Requirement: EXTRANEOUS: Legacy V0.1 seed script. Superseded by agile_backlog.json.

### archive/V0_1/business/*.pdf, *.docx
- Purpose: Legacy V0.1 business documents (business model, IP license, QA report, HF build failure).
- Requirement: EXTRANEOUS: Historical archive. No active requirement.

---

## Summary: Key Findings

### Dead Code (Active Path Does Not Execute)
1. **ChatWindow.tsx** -- App.tsx routes to FindCareApp, so ChatWindow is not rendered. However, ChatWindow has MORE features than FindCareApp (retry, timeout modal, GUIManager, SelectionManager integration, summary message rendering).
2. **GUIManager.tsx** -- Only imported by ChatWindow.tsx (dead). FindCareApp has its own simpler inline implementation.
3. **MessageBubble.tsx** -- Only imported by ChatWindow.tsx (dead). FindCareApp renders HTML inline without Markdown.
4. **SelectionManager.tsx** -- Only imported by ChatWindow.tsx (dead). FindCareApp has its own inline selection UI that is less complete.

### Feature Regression Risk
FindCareApp.tsx (active) is MISSING features that ChatWindow.tsx (dead) has:
- No retry logic for rate limits
- No timeout modal with continue/abandon
- No GUIManager integration (no pagination Back/Forward buttons in control frame)
- No MessageBubble (no Markdown rendering, no GFM tables)
- No action links (#action:filter, #action:next-page)
- No summary message rendering (FC-MSG-001, FC-MSG-002)
- No build/version banner display
- No session lock/unlock (SAFETY-UNLOCK-001)

### Extraneous Files (All brain/machine_artifacts/code/ and several ops/tools/)
All 9 files in brain/machine_artifacts/code/ are one-time scripts that modify .iteration_cache/plan_tree.json (legacy planning artifact). They are safe to leave in archive but serve no ongoing purpose.

Several ops scripts (add_*_bug.py, ask_gpt_*.py, gen_*.py, update_*.py, disk_cleanup.py, cleanup_path_registry.py) are one-time utilities with no ongoing operational purpose.

### Missing Requirements (High Priority)
1. **CI/CD workflows** -- No explicit requirements for deploy-*.yml, promote-*.yml, test.yml in the backlog.
2. **Shared Services deployment** -- No explicit SDT story for deploy-shared-services.yml.
3. **Build numbering** -- bump_build.py has no explicit requirement.
4. **Claude Code governance hooks** -- chathealthy_devops_boot.py and bash_rule_guard.py have no explicit backlog requirements despite being critical governance infrastructure.
5. **LLM client abstraction** -- llm_client.py has no explicit requirement.
6. **Database utilities** -- ChatHealthyMongoUtilities.py has no explicit requirement despite being used by all services.
7. **Agent framework** -- base_agent.py, base_tool.py, tool_registry.py have no requirements.
8. **UX-GEN-001** -- generate_index_page.py does not exist yet. index.html is hand-maintained.
9. **Design documentation leaf pages** -- chat-app-design.html, embedding-design.html, ops-manager-design.html, provider-data-load.html, load-perf-report.html have no backlog requirements.
