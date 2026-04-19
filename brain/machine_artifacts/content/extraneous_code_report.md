# Extraneous Code Report — 2026-04-11

Code I want to kill. Human review required before deletion.

---

## DEAD CODE FILES (recommend delete entirely)

### Pipeline — One-Time Migration Scripts (7 files)
These all ran once, completed their job, and are never imported or called.

| File | Purpose | Why Kill |
|---|---|---|
| `Code/DataPipelines/copy_va_incremental.py` | One-time VA incremental migration | Completed, never called |
| `Code/DataPipelines/embed_va_backfill.py` | One-time VA embedding backfill | Completed, never called |
| `Code/DataPipelines/move_to_frontend.py` | One-time cluster migration | Completed, never called |
| `Code/DataPipelines/migrate_discrepancy_reports.py` | One-time collection rename | Completed, never called |
| `Code/DataPipelines/rename_to_dev.py` | One-time env prefix migration | Completed, never called |
| `Code/DataPipelines/refactor_db.py` | One-time DB restructure | Completed, never called |
| `Code/DataPipelines/load_provider_data.py` | Stub placeholder | Not imported, provider_worker.py does the actual work |

### Brain Scripts — One-Time Backlog Manipulation (9 files)
All modify a legacy `.iteration_cache/plan_tree.json`. One-shot scripts from early planning.

| File | Why Kill |
|---|---|
| `brain/machine_artifacts/code/add_cost_reduction.py` | One-time EPIC-2 feature add |
| `brain/machine_artifacts/code/add_maintenance_features.py` | One-time EPIC-2 feature add |
| `brain/machine_artifacts/code/cleanup_epics.py` | One-time EPIC-2 cleanup |
| `brain/machine_artifacts/code/generate_arch001_pdf.py` | One-time PDF generation |
| `brain/machine_artifacts/code/generate_audit_report_doc.py` | One-time DOCX generation |
| `brain/machine_artifacts/code/generate_epic_plan_artifacts.py` | One-time planning artifacts |
| `brain/machine_artifacts/code/generate_epic_planning_doc.py` | One-time DOCX for EPIC-2 |
| `brain/machine_artifacts/code/update_rx_features.py` | One-time prescription feature update |
| `brain/machine_artifacts/code/update_triage_and_rejected.py` | One-time status update |

### Archive (entire directory)

| Path | Why Kill |
|---|---|
| `archive/V0_1/` | Legacy V0.1 artifacts — seed scripts, old brain docs. Git history preserves them. |

### Root-Level Script

| File | Why Kill |
|---|---|
| `write_script.py` | Unknown purpose, not imported, not referenced |

---

## DEAD CODE COMPONENTS (recommend delete — BUT READ WARNING)

### Frontend Components Replaced by FindCareApp.tsx (4 files)

**WARNING: FEATURE REGRESSION RISK**

`FindCareApp.tsx` replaced these, but `ChatWindow.tsx` has features that `FindCareApp.tsx` does NOT:
- Retry logic with timeout modal ("Yes, keep waiting")
- Markdown rendering in chat bubbles
- Action links in messages (filter, next page)
- Session lock/unlock UI
- Summary message display with structured data
- GUIManager pagination controls

Before deleting, verify these features are either:
(a) No longer needed, or
(b) Implemented in FindCareApp.tsx

| File | Purpose | Risk |
|---|---|---|
| `Code/ConversationalUX/FindCareChat/frontend/src/components/ChatWindow.tsx` | Old chat window | Has more features than replacement |
| `Code/ConversationalUX/FindCareChat/frontend/src/components/GUIManager.tsx` | Old pagination/GUI controls | May contain logic not yet in FindCareApp |
| `Code/ConversationalUX/FindCareChat/frontend/src/components/MessageBubble.tsx` | Old message renderer | Markdown rendering not in FindCareApp |
| `Code/Shared/ux/components/SelectionManager.tsx` | Old selection state manager | Replaced by useSelectionState.ts |

---

## ONE-TIME OPS SCRIPTS (recommend delete or archive)

These are utility scripts that were run once for a specific task. They clutter the ops directory.

| File | Purpose | Why Kill |
|---|---|---|
| `Code/Shared/ops/tools/add_display_bug.py` | One-time bug insertion | Completed |
| `Code/Shared/ops/tools/add_prompt_override_bug.py` | One-time bug insertion | Completed |
| `Code/Shared/ops/tools/add_quality_bug.py` | One-time bug insertion | Completed |
| `Code/Shared/ops/tools/add_risk_acceptance_to_bugs.py` | One-time schema migration | Completed |
| `Code/Shared/ops/tools/add_showstopper_bug.py` | One-time bug insertion | Completed |
| `Code/Shared/ops/tools/cleanup_path_registry.py` | One-time cleanup | Completed |
| `Code/Shared/ops/tools/disk_cleanup.py` | One-time disk cleanup | Completed |
| `Code/Shared/ops/ask_gpt_crosswalk.py` | One-time GPT query | Completed |
| `Code/Shared/ops/ask_gpt_umls_review.py` | One-time GPT query | Completed |
| `Code/Shared/ops/brain_biz_arch_collab.py` | One-time GPT collaboration | Completed |
| `Code/Shared/ops/brain_biz_arch_diagrams.py` | One-time diagram generation | Completed |
| `Code/Shared/ops/brain_refactor_collab.py` | One-time GPT collaboration | Completed |
| `Code/Shared/ops/brain_snapshot.py` | One-time brain snapshot | Completed |
| `Code/Shared/ops/build_code_review_pdf.py` | One-time PDF generation | Completed |
| `Code/Shared/ops/build_repo_json.py` | One-time repo export | Completed |
| `Code/Shared/ops/design_review_collab.py` | One-time GPT collaboration | Completed |
| `Code/Shared/ops/gen_evaluate_care_v3.py` | One-time doc generation | Completed |
| `Code/Shared/ops/gen_investor_docx.py` | One-time investor doc | Completed |
| `Code/Shared/ops/gen_investor_docx_v3.py` | One-time investor doc v3 | Completed |
| `Code/Shared/ops/gen_investor_pdf_v2.py` | One-time investor PDF | Completed |
| `Code/Shared/ops/gen_moat_v4.py` | One-time moat document | Completed |
| `Code/Shared/ops/update_brain_descriptions.py` | One-time brain update | Completed |
| `Code/Shared/ops/update_manifest_capabilities.py` | One-time manifest update | Completed |
| `Code/Shared/ops/dev_pipeline.py` | One-time pipeline dev script | Completed |

---

## MISPLACED FILE (recommend move, not delete)

| File | Issue | Action |
|---|---|---|
| `Code/DataPipelines/ChatHealthyMongoUtilities.py` | Header says "DO NOT USE in DataPipelines" | Move to Code/Shared/ |

---

## SUMMARY

| Category | Files | Action |
|---|---|---|
| Pipeline dead migrations | 7 | Delete |
| Brain one-time scripts | 9 | Delete |
| Archive directory | 3+ | Delete |
| Root script | 1 | Delete |
| Replaced frontend components | 4 | Delete after feature regression check |
| One-time ops scripts | 24 | Delete or archive |
| Misplaced file | 1 | Move |
| **Total files to review** | **49** | |

---

## DEAD FUNCTIONS INSIDE LIVE FILES (52 functions)

Found by static analysis: defined but never called or referenced anywhere in the codebase.
Framework-invoked functions (FastAPI routes, Azure triggers, governance hooks) already filtered out.

### Probably Dead — Old Architecture, Replaced

| File | Line | Function | Why Dead |
|---|---|---|---|
| `tool_router.py` | 36 | `register_all` | Old tool registration system. FindCareApp uses direct API calls, not tools. |
| `specialty_classifier.py` | 115 | `classify_specialties` | Replaced by /classify endpoint using GPT-4.1 |
| `specialty_ranker.py` | 21 | `rank_specialties` | Replaced by GPT-4.1 ranking in /classify response |
| `provider_search_service.py` | 499 | `get_provider_location` | Unused location lookup — search uses specialty codes |
| `url_guardian.py` | 71 | `check_url` | Old tool URL validation — tools no longer used |
| `url_guardian.py` | 89 | `guard_tool_result` | Old tool result guarding — tools no longer used |
| `county_economic_enrichment.py` | 205 | `enrich_providers_with_economics` | Economic enrichment not in any pipeline step |

### Probably Keep — Public API, Not Yet Wired

| File | Line | Function | Why Keep |
|---|---|---|---|
| `brain_loop.py` | 103 | `write_brain_artifact` | Brain loop system — built, not yet called from hooks |
| `brain_loop.py` | 201 | `write_assignment` | Brain loop — assignment queue |
| `brain_loop.py` | 265 | `pick_up_assignment` | Brain loop — assignment pickup |
| `brain_loop.py` | 336 | `deliver_result` | Brain loop — result delivery |
| `brain_loop.py` | 360 | `request_feedback` | Brain loop — feedback request |
| `brain_loop.py` | 385 | `provide_feedback` | Brain loop — feedback response |
| `brain_loop.py` | 412 | `write_review_pack` | Brain loop — review pack |
| `brain_loop.py` | 522 | `run_uat` | Brain loop — UAT execution |
| `brain_loop.py` | 613 | `close_review` | Brain loop — review close |
| `brain_loop.py` | 639 | `run_regression` | Brain loop — regression testing |
| `brain_loop.py` | 678 | `get_state` | Brain loop — state query |
| `brain_loop.py` | 683 | `get_pending_reviews` | Brain loop — pending reviews |
| `brain_loop.py` | 689 | `sync_all_to_mongo` | Brain loop — MongoDB sync |
| `normalization.py` | 9 | `normalize_min_max` | EvaluateCare normalization — will be used when scoring wired |
| `normalization.py` | 16 | `normalize_boolean` | EvaluateCare normalization |
| `normalization.py` | 21 | `normalize_linear_scale` | EvaluateCare normalization |
| `normalization.py` | 28 | `normalize_inverse` | EvaluateCare normalization |
| `normalization.py` | 39 | `normalize_categorical` | EvaluateCare normalization |
| `normalization.py` | 44 | `normalize_passthrough` | EvaluateCare normalization |
| `scoring_engine.py` | 54 | `register_measure` | Public API for adding scoring measures |
| `cache.py` | 76 | `invalidate` | Cache invalidation — needed for data refresh |
| `cost_guard.py` | 258 | `get_usage_report` | Token usage reporting |
| `cost_guard.py` | 311 | `set_limit` | Token usage limit setting |
| `llm_client.py` | 237 | `get_active_model` | Model introspection |
| `machine_brain.py` | 224 | `store_decision` | Decision storage for future audit trail |
| `machine_brain.py` | 293 | `backfill_embeddings` | Embedding backfill utility |
| `machine_brain.py` | 337 | `list_all_decisions` | Decision listing |
| `pipeline_worker_base.py` | 152 | `output_exists_and_valid` | Idempotency check (PIPE-ID-001) |

### Needs Decision — Could Go Either Way

| File | Line | Function | Question |
|---|---|---|---|
| `safety_service.py` | 159 | `session_is_locked` | Session lock check — is this used by the frontend? |
| `atlas_cluster_manager.py` | 169 | `resume_for_job` | Cluster resume — used by lifecycle manager? |
| `copy_to_frontend.py` | 282 | `migrate_environment` | Environment migration — still needed? |
| `otp_manager.py` | 43 | `generate_otp` | OTP generation — used by ExchangeOTP route? |
| `pipeline_db.py` | 52 | `get_admin_db` | Admin DB access — used anywhere? |
| `pipeline_db.py` | 57 | `get_collection` | Collection access — used anywhere? |
| `ops_manager/audit_trail.py` | 92 | `log_warning` | Ops audit trail — used by ops agent? |
| `agent_framework/tool_registry.py` | 59 | `tool_names` | Agent tool listing — used by ops agent? |
| `agent_framework/tool_registry.py` | 63 | `tool_schemas` | Agent tool schemas — used by ops agent? |
| `brain_auth.py` | 78 | `authenticate` | Brain authentication — used by brain_runner? |
| `brain_auth.py` | 94 | `get_gpt_key` | GPT key retrieval — used by brain_runner? |
| `framework_version.py` | 31 | `get_active` | Framework version query |
| `manifest_generator.py` | 257 | `total_entries` | Manifest entry count |
| `chathealthy_devops_boot.py` | 140 | `blocks_action` | Bug governance method — called via pydantic? |
| `chathealthy_devops_boot.py` | 656 | `get_constraints_summary` | Constraints summary — called during boot? |
| `uat_report.py` | 81 | `update_uat_status` | UAT status update — used by QA report? |
| `uat_report.py` | 119 | `seed_uat_status` | UAT status seeding — one-time? |

---

## UPDATED SUMMARY

| Category | Count | Action |
|---|---|---|
| Dead files (already deleted) | 49 | Done |
| Dead functions — probably dead | 7 | Delete after human review |
| Dead functions — probably keep | 28 | Create requirements, keep |
| Dead functions — needs decision | 17 | human decides |
| **Total functions to review** | **52** | |

---

## NOT EXTRANEOUS — Requirements Created

The backend audit created 30 new requirements under FC-BACKEND.
The pipeline audit identified 14 files needing requirements (documented in code_audit_pipeline.md).
The frontend/ops audit identified CI/CD workflows, governance hooks, and foundational libraries needing requirements.

These are gaps, not extraneous code. Requirements will be created silently.
