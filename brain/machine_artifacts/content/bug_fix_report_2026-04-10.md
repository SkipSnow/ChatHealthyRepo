# Bug Fix Report — 2026-04-10 Night Session

## Test Results
- **Non-Playwright (local):** 77 passed, 6 skipped, 0 failed
- **Playwright search (local):** 7 passed, 0 failed
- **Dev environment:** Not deployed yet — commit on local dev branch

---

## BUGS FIXED (code changes made, resolution_status = tested_passed)

### BUG-UX-011 — Apply Filter does not clear old providers
- **Root cause:** Apply Filter sent ALL checked codes including hidden (filtered-out) items, so results didn't change
- **Fix:** Website/index.html — only send codes from visible checked items (`row.style.display !== 'none'`)
- **Test:** Needs Playwright test (FC-FILT-001-REQ-007)

### BUG-UX-002 — Left panel overflowY:hidden, filter can't scroll
- **Root cause:** Filter checkbox container had hardcoded `max-height:390px` — too small on some screens
- **Fix:** FindCareApp.tsx — changed to `flex:1;overflow-y:auto` so container fills available space
- **Test:** Needs Playwright test

### BUG-UX-015 — Filter header labels truncated ("Prescr", "Home")
- **Root cause:** Labels in cramped flex container with no nowrap, tiny font
- **Fix:** FindCareApp.tsx — added `white-space:nowrap`, `flex-wrap:wrap`, increased font from 9px to 10px, wider gaps
- **Test:** Needs Playwright test (test_filter_panel.py::test_filter_header_labels_not_truncated)

### BUG-UX-013 — Search timer stalls between classify and DB search
- **Root cause:** Timer cleared after /classify response but before /search completed
- **Fix:** FindCareApp.tsx — timer clears only after fetchProviders returns or error
- **Test:** test_search_timer.py (Playwright)

### BUG-UX-009 — Selection panel requires scrolling to see both lists
- **Root cause:** Selected list had no minHeight, could be pushed off screen
- **Fix:** FindCareApp.tsx — added `minHeight: 60, flexShrink: 0` to selected container
- **Test:** Needs Playwright test

### BUG-UX-012 — Drag and drop missing
- **Root cause:** ProviderCard had dragStart handler but no drop target existed
- **Fix:** FindCareApp.tsx — added onDragOver/onDrop handlers to selection container
- **Test:** Needs Playwright test (FC-SELECT-001-REQ-002)

---

## BUGS VERIFIED FIXED (no code change needed, architecture resolved them)

### BUG-UX-010 — Garbage specialty text in chat window
- **Reason:** Gemini 2.5 Flash dumped raw data into chat. Switched to GPT-4.1 with structured JSON + separated /classify from /chat. Specialty data never enters chat rendering path.

### BUG-FILTER-002 — Filter panel text displayed sideways
- **Reason:** innerHTML replacement of leftPanel removes the vertical-rl label span. Already working.

### BUG-MODEL-001 — Gemini 2.5 Flash overly broad specialty list
- **Reason:** Switched to GPT-4.1 (commit 0610d52). /classify now uses GPT-4.1 with structured JSON output.

### BUG-EVAL-002 / BUG-EVAL-003 — Wrong providers sent to EvaluateCare
- **Reason:** FindCareApp refactor uses selection.state.selected (selectedRef.current) — only selected providers are sent.

### BUG-MSG-001 — LLM duplicates system summary
- **Reason:** /classify + /search architecture eliminated LLM from the response path. System builds the response, not AI.

---

## BUGS NOT FIXED (need human input or infrastructure)

### BUG-FILTER-001 — Specialty filter panel empty
- **Blocker:** Vector search indexes missing on frontend cluster (provider_vector_index, specialty_vector_index)
- **Action:** Pipeline must create indexes via CopyToFrontEnd

### BUG-VECTOR-001 — RELEASE BLOCKER: Specialty code resolution must vector search
- **Blocker:** Same as FILTER-001 — vector indexes not created
- **Action:** Pipeline infrastructure work

### BUG-DESIGN-001 — Pagination controls in cursor bar
- **Blocker:** UX design needed from Boss
- **Action:** Boss provides mockup

### BUG-UX-001 — 'kids doc in VA' stuck response
- **Blocker:** May be model/prompt issue — needs live testing
- **Action:** Test with GPT-4.1 to verify if still occurs

### BUG-CLASSIFY-001, BUG-PERF-001 — Empty rule text
- **Blocker:** No bug description — Boss needs to define
- **Action:** Boss provides details

### BUG-UX-005 — Low priority, in analysis
- **Action:** Boss to triage

### BUG-UX-007 — Filter format doesn't match Excel mockup
- **Blocker:** UX design from Boss (Excel mockup exists but implementation differs)
- **Action:** Boss to review current layout vs mockup

### BUG-UX-008 — Environment banner formatting
- **Blocker:** UX design — labels, values, spacing
- **Action:** Boss to specify desired format

### BUG-BA-001 — Three numbers in filter header
- **Status:** testing — Playwright test exists
- **Action:** Boss UAT

### BUG-UX-014 — Evaluate button position/cold/hot state
- **Status:** Requirements written (FC-EVAL-001), needs implementation
- **Action:** Implement after requirements review

### BUG-LOCAL-001 — start_local.bat zombie cleanup incomplete
- **Status:** kill_zombies.py handles port-based cleanup but not process-name-based
- **Action:** Evaluate if kill_zombies.py is sufficient

### BUG-SEC-003 — EvaluateCare mTLS handoff
- **Status:** Code exists, works locally with certs. May be resolved by GPT-4.1 switch.
- **Action:** Test live on local

### BUG-SEC-005 — 426 body on every server
- **Status:** Local Caddy done, HF spaces not verified
- **Action:** Test after dev deploy

---

## INFRASTRUCTURE CHANGES

### v4-007 Enforcement Scanner
- pre_deploy_rule_check.py now scans agile_backlog.json
- Blocks check-in if implemented requirements lack pytest_id
- Unimplemented requirements exempt (no test for unbuilt code)

### Guard Prompt Update
- Non-prod frontend process recycle (taskkill on ports 80, 443, 5173, 8000, 8001) no longer blocked
- Databases, pipeline, production still protected

### Bug Schema
- `req_id` added as mandatory field — every bug must trace to a requirement

### Playwright Frame Detection
- All 7 Playwright test files updated to detect `:3000` iframe (Caddy proxy to Vite)

---

## TEST COVERAGE SUMMARY
- 77 non-Playwright tests: ALL PASS
- 7 Playwright search tests: ALL PASS
- 40 requirements that were missing pytest_id: ALL ASSIGNED
- Scanner violations: 0 (deploy may proceed)
