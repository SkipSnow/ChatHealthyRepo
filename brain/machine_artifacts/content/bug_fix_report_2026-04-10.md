# QA Report — What To Test
## Updated: 2026-04-11

### What Works Right Now (test on https://localhost)

1. **Search** — type a question, get providers. "find me a shrink in delaware" works.
2. **Filter panel** — specialties appear on left, checkboxes work
3. **Apply Filter** — uncheck a specialty, click Apply, results change
4. **Banner** — shows Version, Framework, Build, Commit with labels
5. **Drag and drop** — drag a provider from top list to bottom evaluate container
6. **Timer** — counts continuously during search, clears when results appear
7. **426 rejection** — HTTP requests properly rejected with status code in body

### Bugs Closed (3) — Done, no action needed

| Bug | What |
|---|---|
| BUG-UX-013 | Timer stall — fixed |
| BUG-UX-012 | Drag and drop — fixed |
| BUG-UX-008 | Banner formatting — fixed |

---

## READY FOR YOUR UAT (18)

These are fixed in code. Test them and tell me pass or fail.

| Bug | What to test | How |
|---|---|---|
| BUG-UX-011 | Apply Filter clears old providers | Uncheck a specialty, click Apply, verify list changes |
| BUG-UX-002 | Filter panel scrolls | Load enough specialties, scroll the checkbox list |
| BUG-UX-015 | Filter labels not truncated | Check "Prescribers" and "Homeopathic" labels are fully visible |
| BUG-MSG-001 | No duplicate summary | Search, verify one summary message not two |
| BUG-MSG-002 | Summary uses your words | Search "shrinks", verify it says "shrinks" not "psychiatrists" |
| BUG-FILTER-002 | Filter not sideways | Filter text is horizontal, not rotated |
| BUG-UX-003 | Stop button not orphaned | Timer and stop button move together |
| BUG-UX-004 | Prescriber toggle stays checked | Toggle prescriber, verify it stays in correct state |
| BUG-UX-010 | No garbage text | Search, verify no raw specialty list in chat |
| BUG-MODEL-001 | Good specialty results | Search "headache", verify relevant specialties not random ones |
| BUG-EVAL-002 | Correct providers sent to evaluate | Select 1 provider, evaluate, verify only 1 sent |
| BUG-EVAL-003 | Same as above | Pick 1, verify 1 shows in evaluate |
| BUG-CODE-001 | Clean frontend | General — app works, no visual glitches |
| BUG-SEC-003 | EvaluateCare handoff | **KNOWN ISSUE: needs server restart for is_hf fix** |
| BUG-LOCAL-001 | start_local.bat cleanup | Restart local, verify no zombies |
| BUG-PIPE-002 | Pipeline try/finally | Pipeline running MS right now — will verify on completion |
| BUG-PIPE-008 | Both vector indexes | Already verified — health shows "ok" |
| UAT-FILTER-001 | Filter functionality overall | Checkboxes, toggles, counts |

---

## NOT READY — Don't Test Yet (35 fixing + 39 open)

These are either being worked on or need design input from you. Don't waste time on them.

**Needs your design input:**
- BUG-DESIGN-001 — Pagination controls placement
- BUG-UX-007 — Filter format vs Excel mockup
- BUG-UX-014 — Evaluate button position/cold/hot state
- BUG-CLASSIFY-001, BUG-PERF-001 — Empty bug descriptions, need your input

**Needs server restart to test:**
- BUG-SEC-003 — EvaluateCare handoff (is_hf fix not loaded)

**Infrastructure / pipeline (will resolve when pipeline completes):**
- BUG-FILTER-001, BUG-VECTOR-001 — Vector indexes (now created)
- BUG-LOAD-001, BUG-LOAD-002 — Provider load (MS running now)
- BUG-DATA-001 — Frontend data insufficient

**Regression test quality (my cleanup):**
- BUG-REG-001 — 91 criteria to reach 100% regression
- BUG-TEST-001 through BUG-TEST-030 — Individual test fixes

---

## Pipeline Status

FindCarePipeline running: MS full load with SpecialtyMetaData + CopyToFrontEnd
Instance: 7f6e34d2e7264908897b5b594bb9ff75
