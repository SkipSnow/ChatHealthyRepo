# QA Report — 2026-04-11
## https://localhost

---

## YOUR UAT (17) — sorted by severity

| Severity | Bug | What to test |
|---|---|---|
| medium | BUG-MSG-003 | Message text wrong |
| medium | BUG-FILTER-002 | Filter text horizontal not sideways |
| medium | BUG-UX-002 | Filter panel scrolls |
| medium | BUG-UX-003 | Stop button not orphaned |
| medium | BUG-UX-004 | Prescriber toggle stays in correct state |
| medium | BUG-UX-010 | No garbage text in chat |
| medium | BUG-MODEL-001 | Relevant specialties for search |
| medium | BUG-EVAL-002 | Only selected providers sent to evaluate |
| medium | BUG-EVAL-003 | Pick 1, only 1 shows in evaluate |
| medium | BUG-CODE-001 | General — app works, no visual glitches |
| medium | BUG-PIPE-002 | Pipeline try/finally (running now) |
| medium | BUG-PIPE-008 | Both vector indexes (verified ok) |
| medium | UAT-FILTER-001 | Filter functionality overall |
| medium | UAT-UX-001 | Timer + stop button together |
| medium | BUG-BA-001 | Three filter numbers are specialty counts |

---

## CLAUDE WORKING (37) — sorted by severity

| Severity | Bug | Status |
|---|---|---|
| show_stopper | BUG-GOV-005 | Requirements without pytests — scanner built |
| show_stopper | BUG-UX-011 | Apply Filter — implementing human rewrite of requirement |
| critical | BUG-SEC-005 | 426 body on every server |
| critical | BUG-UX-014 | Evaluate button cold/hot — just implemented, needs Playwright test |
| critical | BUG-UX-015 | Filter labels truncated — fix applied |
| critical | BUG-REG-001 | 69 regression criteria — stabilizing tests |
| high | BUG-GOV-006 | Guard structured I/O — done |
| high | BUG-TEST-001 to 030 | 30 individual pytest fixes |

---

## NEEDS YOUR INPUT (39) — sorted by severity

| Severity | Bug | What I need |
|---|---|---|
| show_stopper | BUG-LOAD-001 | DE provider load — pipeline running MS now |
| show_stopper | BUG-LOAD-002 | Provider count wrong — depends on pipeline |
| show_stopper | BUG-DATA-001 | Frontend data insufficient — depends on pipeline |
| show_stopper | BUG-PIPE-010 | Quality pipeline ships without embeddings |
| critical | BUG-VECTOR-001 | Vector search — indexes now created, needs retest |
| critical | BUG-LEGAL-001 | CPT codes consumer-facing restriction |
| critical | BUG-002 | Empty description — what's the issue? |
| medium | BUG-DESIGN-001 | Pagination controls — where do they go? |
| medium | BUG-UX-007 | Filter format vs Excel mockup |
| medium | BUG-UX-001 | 'kids doc in VA' stuck — needs live test |
| medium | BUG-CLASSIFY-001 | Empty — what's the issue? |
| medium | BUG-PERF-001 | Empty — what's the issue? |
| medium | BUG-001 | Empty — what's the issue? |
| medium | BUG-GOV-002 | human constraint check |
| low | BUG-UX-005 | Low priority |

---

## Pipeline Status

FindCarePipeline running: MS full load + SpecialtyMetaData + CopyToFrontEnd
