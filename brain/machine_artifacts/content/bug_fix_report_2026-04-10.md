# QA Report — Local Environment
## Generated: 2026-04-11

### Test Results
- **Non-Playwright (local):** 63 passed, 6 skipped, 0 failed
- **Playwright (local):** Apply Filter test PASSED (51s)
- **Architecture constraint test:** 4 passed (lab DB, vectors, index, risk acceptance)
- **Full regression:** 48 pass, 30 fail, 61% pass rate (BUG-REG-001 tracking)
- **Security regression (EPIC-002):** 7 pass, 0 fail, 100%
- **FindCare health:** status ok, both vector indexes created

---

## CLOSED (Boss UAT signed off)

| Bug | Description |
|---|---|
| BUG-UX-013 | Search timer stalls between classify and DB search — FIXED, Boss closed |

---

## TESTED_PASSED (awaiting Boss UAT)

| Bug | Description |
|---|---|
| BUG-MSG-001 | LLM duplicates system summary — fixed by /classify + /search architecture |
| BUG-FILTER-002 | Filter panel text sideways — fixed by innerHTML replacement |
| BUG-MSG-002 | Summary used LLM term instead of user term — fixed by architecture |
| BUG-UX-002 | Left panel can't scroll — fixed (flex:1 replaces max-height:390px) |
| UAT-FILTER-001 | Filter panel functionality — signed off, UX pending |
| UAT-UX-001 | Timer + Stop button in control frame — working |
| BUG-UX-003 | Stop button orphaned in chat — fixed |
| BUG-UX-004 | Prescriber toggle re-rendered checked — fixed |
| BUG-UX-006 | Evaluate handoff display — fixed |
| BUG-CODE-001 | Bad frontend coding practice — refactored to FindCareApp.tsx |
| BUG-EVAL-002 | Wrong providers sent to EvaluateCare — fixed by selection state |
| BUG-SEC-003 | mTLS handoff fails — fixed by GPT-4.1 switch + cert config |
| BUG-EVAL-003 | Picked 1 but 5 showed — fixed by selection state |
| BUG-UX-010 | Garbage specialty text in chat — fixed by architecture change |
| BUG-MODEL-001 | Gemini overly broad specialties — fixed by GPT-4.1 switch |
| BUG-UX-011 | Apply Filter doesn't clear old providers — FIXED + Playwright test passes |
| BUG-PIPE-002 | No try/finally in CopyToFrontEnd — FIXED, pipeline running |
| BUG-PIPE-008 | Missing specialty vector index — FIXED, both indexes created |
| BUG-LOCAL-001 | start_local.bat zombie cleanup — tested |

---

## FIXING (code changes in progress)

| Bug | Description |
|---|---|
| BUG-SEC-005 | 426 body must include status code on every server |
| BUG-GOV-005 | Requirements without pytests (91 criteria in BUG-REG-001) |
| BUG-UX-012 | Drag and drop missing (drop target added, needs Playwright test) |
| BUG-UX-014 | Evaluate button position/cold/hot state (requirements written) |
| BUG-UX-015 | Filter header labels truncated (fix applied, needs Playwright verify) |
| BUG-GOV-006 | Guard GPT calls lack structured I/O (schema added) |
| BUG-REG-001 | 91 regression criteria to reach 100% |
| BUG-TEST-001 through BUG-TEST-030 | Individual failing pytests |

---

## OPEN (needs analysis or human input)

| Bug | Description |
|---|---|
| BUG-FILTER-001 | Filter panel empty — vector indexes now created, needs retest |
| BUG-VECTOR-001 | Vector search release blocker — indexes now created |
| BUG-DESIGN-001 | Pagination controls in cursor bar — UX design from Boss |
| BUG-UX-001 | 'kids doc in VA' stuck response — needs live test with GPT-4.1 |
| BUG-CLASSIFY-001 | Empty rule text — needs Boss input |
| BUG-PERF-001 | Empty rule text — needs Boss input |
| BUG-UX-005 | Low priority, in analysis |
| BUG-UX-007 | Filter format doesn't match Excel mockup |
| BUG-UX-008 | Environment banner formatting |
| BUG-UX-009 | Selection panel requires scrolling (minHeight fix applied) |
| BUG-BA-001 | Three numbers in filter header — in testing |
| BUG-SEC-001 | HF Spaces still use bearer tokens |
| BUG-SEC-002 | mTLS between FindCare and EvaluateCare |
| BUG-SEC-004 | Dev HF servers not tested for HTTPS |
| BUG-GOV-002 | Boss constraint check in guard |
| BUG-GOV-003 | Guard blocks authorized actions |
| BUG-GOV-004 | DR-008 regex false positives |
| BUG-ROADMAP-001 | Roadmap milestones disconnected |
| BUG-LOAD-001 | Delaware provider load incomplete |
| BUG-LOAD-002 | Provider count wrong |
| BUG-LOAD-003 | Source data files must not be deleted |
| BUG-LEGAL-001 | CPT codes cannot be consumer-facing |

---

## INFRASTRUCTURE CHANGES (this session)

- v4-030: Epics business-aligned, 6 IT epics dissolved
- v4-031: No dead code — 51 functions commented out, scanner enforces
- v4-032: Plural/singular naming convention
- Schema enforcement: agile_backlog, bugs, ai_operations — all strict
- Regression runner: asyncio concurrent (16 threads, 50ms stagger)
- Pipeline trigger: proper auth via pipeline.http, monitor via status URL
- Copyright: Skip Snow → ChatHealthy.ai LLC (239 files)
- EPIC-008 Architecture created (operational, empty)
- FindCarePipeline: Step 2 SpecialtyMetaData + Step 10 CopyToFrontEnd
- Both vector indexes created on frontend cluster
- realized_by populated on 74 features and 1,891 requirements
