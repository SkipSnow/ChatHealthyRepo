# Extraneous Code Report — 2026-04-11

Code I want to kill. Boss review required before deletion.

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

## NOT EXTRANEOUS — Requirements Created

The backend audit created 30 new requirements under FC-BACKEND.
The pipeline audit identified 14 files needing requirements (documented in code_audit_pipeline.md).
The frontend/ops audit identified CI/CD workflows, governance hooks, and foundational libraries needing requirements.

These are gaps, not extraneous code. Requirements will be created silently.
