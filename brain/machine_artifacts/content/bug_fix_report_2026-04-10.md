# QA Report — 2026-04-11
## https://localhost (servers restarted, latest code)

---

## YOUR UAT — Test These Now

| Bug | What to test | How |
|---|---|---|
| BUG-COMM-001 | Evaluate handoff works | Select providers, click Evaluate These Providers, no error |
| BUG-UX-011 | Apply Filter refreshes results | Uncheck a specialty, click Apply, list changes |
| BUG-UX-002 | Filter panel scrolls | Search something with many specialties, scroll the list |
| BUG-UX-015 | Filter labels readable | "Prescribers" and "Homeopathic" not truncated |
| BUG-UX-010 | No garbage text in chat | Search, no raw specialty list visible |
| BUG-MODEL-001 | Good specialty results | Search "headache", relevant specialties appear |
| BUG-EVAL-002 | Only selected providers sent | Select 1, evaluate, verify only 1 shows |
| BUG-MSG-001 | No duplicate summary | One summary message, not two |
| BUG-MSG-002 | Uses your words | Search "shrinks", says "shrinks" not "psychiatrists" |
| BUG-BA-001 | Three numbers correct | Filter header shows specialty type counts, not provider counts |
| UAT-FILTER-001 | Filter overall | Checkboxes, toggles, Apply, counts |

---

## CLAUDE FIXES — Don't Test Yet

| Bug | What I'm doing |
|---|---|
| BUG-REG-001 | 91 regression criteria — stabilizing all tests |
| BUG-TEST-001 to 030 | Individual pytest fixes |
| BUG-GOV-005 | Missing pytests on requirements |
| BUG-GOV-006 | Guard structured I/O (done, needs verify) |
| BUG-SEC-005 | 426 body on all servers/environments |
| BUG-UX-009 | Selection panel scroll (minHeight fix applied) |

---

## NEEDS YOUR INPUT — Can't Fix Without You

| Bug | What I need |
|---|---|
| BUG-DESIGN-001 | Where do pagination controls go? Mockup needed |
| BUG-UX-007 | Filter format — does it match your Excel mockup? |
| BUG-UX-014 | Evaluate button — position, cold/hot state, popup. Requirements written, need your approval to build |
| BUG-CLASSIFY-001 | Empty bug — what's the issue? |
| BUG-PERF-001 | Empty bug — what's the issue? |

---

## Pipeline Status

FindCarePipeline running: MS full load + SpecialtyMetaData + CopyToFrontEnd
