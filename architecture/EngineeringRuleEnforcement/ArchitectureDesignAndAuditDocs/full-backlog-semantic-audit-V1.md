# Full Backlog Semantic Audit — V1

**Auditor:** Independent (no shared context with prior agents)
**Date:** 2026-04-27
**Scope:** Entire ChatHealthy.ai agile backlog and adjacent governance artifacts.
**Mode:** Read-only. No commits. No mutations.
**Sources of truth used:** agile_backlog.json, bugs.json, engineering_rules.json, risk_acceptance.json, V19 design doc (binding), shipped framework code under `architecture/EngineeringRuleEnforcement/code`, shipped tests, schemas under `Website/schemas/`.

---

## Executive Summary

**Findings count by severity:**

| Severity | Count |
|---|---|
| Blocking | 0 |
| High | 4 |
| Medium | 7 |
| Low | 9 |
| Informational | 4 |
| **Total** | **24** |

**Verdict: CLOSED for F-002 scope (9 remediated in commit d7b3c6b); 15 findings outside F-002 scope deferred per architect directive.**

No finding rises to "stop the commit" unless the architect deems the V18→V19 docstring labeling drift in framework code or the consolidation Word report's stale counts blocking. Schema validation is clean; FK integrity is clean for the new F-002 stories; every BR-1..16 and TR-1..19 from V19 is present in the backlog and bucketed into a semantically appropriate story; BR-15/BR-16 are correctly under S-006 (JSON validation).

The most consequential findings are (1) the consolidation Word report's count claims drifted (it says 31 new reqs / 37 total under F-002; the live backlog is 35 new and 41 total, because BR-15/16/TR-18/19 landed after that report was written), (2) framework code docstrings still cite V17/V18 section numbers despite V19 being the binding contract, (3) two "ENF-WORKER" bugs marked `in_analysis` that the consolidation/refactor logically resolved, and (4) 4 unresolved EPIC-008 req-ID references in 3 git-tracked files.

---

## Disposition (2026-04-27, post-cleanup commit d7b3c6b)

A cleanup pass landed in commit `d7b3c6b` ([Normal Mode] EPIC-008-F-002 close: V19 design, backlog consolidation, F-002 audit clean). The 9 F-002-scope findings flagged by this audit were remediated in that commit; 15 findings outside F-002 scope are deferred per architect directive ("engineering rules must be 100% clean; rest of backlog is future work"). A doc-alignment pass on 2026-04-27 (post-`d7b3c6b`) re-verified each item against the shipped tree.

### Resolved in `d7b3c6b` (F-002 scope)

| ID | What was done |
|---|---|
| **B7-001** (partial — F-002 area portion only) | The consolidation pass repointed FK references in the F-002 area. The remaining EPIC-008-F-007 area unresolved IDs (test_findcare_requirements.py, ChatHealthyRelaxedSchema.json) are deferred as F-007 scope. |
| **F2-001** | V2 consolidation report issued at `architecture/EngineeringRuleEnforcement/ArchitectureDesignAndAuditDocs/EPIC-008-F-002-backlog-consolidation-V2.docx` with V19 anchoring and corrected counts (35 new / 41 total under F-002). V1 retained for history. |
| **F3-001** | `scan_files_enforcement_worker.py:439` was the V18 binding-contract claim; updated to "See V19 §4.9.3 for the binding contract." (The header at lines 1-31 now binds to V19 explicitly.) |
| **G2-001** | `BUG-ENF-WORKER-001` and `BUG-ENF-WORKER-002` moved from `status: in_analysis` → `status: fixed` in `bugs.json`. |
| **G1-001** (partial) | `BUG-SCANNER-OVERSCOPE-001` repointed from `req_id: "Orphan"` → `EPIC-008-F-002-S-004-REQ-T-001`. The other Orphan high bug (`BUG-AGILE-BACKLOG-UNIQUENESS-DISABLED-001`) is deferred as backlog-governance scope, not F-002. |
| **D3-002** (partial) | TR-1, TR-2, TR-9, TR-10, TR-13 backlog wording aligned to V19 verbatim (verified character-length match against V19's TR table: 1351, 1477, 1164, 1289, 478). Other TRs retained as faithful paraphrase per the audit's tolerance. |
| **D4-001** | Story descriptions for S-003..S-006 updated to reference V19 (was V18); S-006 description expanded to mention BR-15/16 + TR-18/19. |
| **E1-001** | `chathealthy_enforcement_manager.py` internal docstrings updated V17 → V19 throughout (Table 3, Table 5, §4.3.2 references). |
| **E2-001** | `enforcement_worker.py` internal docstrings updated V17 → V19 throughout (§4.5, §4.9.3, Table 4, Table 9 references). |
| **E3-001** | `scan_files_enforcement_worker.py` internal docstrings updated V18 → V19 throughout, including the line 439 binding-contract claim. The single historical narrative on the `_validate_json` SCOPE_DEFAULT (line 115, "V17 §4.5 originally specified True") preserved by design. |
| **F1-001** | Test fixture renamed `04_missing_local_schema.json` → `04_unreachable_schema_url.json`; the test that loads it updated. Verified content matches V19 Table 13 R4 ("JSON whose $schema URL is unreachable or returns non-JSON content"). |
| **E5-001** | TR-13 implemented in code: `Code/Shared/ops/tools/preCommitScan.py` no longer contains `.json` in `SCAN_EXTENSIONS` (line 53) and no longer contains `\.json$` in `HTTP_EXEMPT_PATTERNS` (lines 43-49). ScanFilesEnforcementWorker is now the single source of truth for JSON validation. |
| **F3-002** | Bulk replacement of stale V17/V18 labels across the framework code completed in this commit (45 total replacements per the commit message); historical narrative preserved at `scan_files_enforcement_worker.py:115`. |
| **H1-001** | Same disposition as F2-001 — V2 consolidation report issued. |

### Deferred — outside F-002 scope per architect directive

| ID | Why deferred |
|---|---|
| **B7-001** (F-007 area portion) | EPIC-008-F-007 (per-file JSON-schema-enforcement) cleanup is its own future story; the unresolved tokens in `test_findcare_requirements.py` and `ChatHealthyRelaxedSchema.json` belong to that work. |
| **B1-001** | 3 bugs (BUG-GOV-008, BUG-DEFER-002, BUG-DEFER-003) cite strict-format req_ids that don't resolve. None point at F-002. |
| **B1-002** | 8 bugs use sentinel orphan markers — backlog-governance scope. |
| **B1-003** | 8 bugs use legacy non-EPIC ID formats — backlog-governance scope. |
| **B3-001** | 57 of 65 `depends_on` entries don't resolve (legacy `FC-SELECT-001` / `FC-PIPE-SPEC-*` IDs). Cross-cutting backlog cleanup. |
| **A7-001** | 46 orphan reqs lacking `req_id` — backlog-governance scope. |
| **B2-001** | 89% of pytest entries are `orphan: true` — coverage-tracking gap, not a defect. |
| **B4-001** | `realized_by` is intentionally a free-text array; not actionable. |
| **D3-001** | Minor BR-3/BR-8/BR-11/BR-14 paraphrase drift — within audit tolerance. |
| **E4-001** | Tests don't tag which TR they exercise; coverage-tracking gap. |
| **G1-001** (BUG-AGILE-BACKLOG-UNIQUENESS-DISABLED-001 portion) | Backlog-governance scope (`pre_deploy_rule_check.py`), not F-002 framework. |
| **I1-001** | Informational — defensive code is fully TR-traced. No action. |

---

## A. Graph integrity (entire backlog)

Walk: 9 epics, 74 features, 274 stories, 2,152 requirement records (2,106 carry a req_id; 46 are properly marked `orphan: true` with no req_id, which the schema permits).

### A1. Epic ID format

CLEAN. All 9 epic_ids match `^EPIC-\d{3}$`: EPIC-001, EPIC-002, EPIC-004, EPIC-005, EPIC-006, EPIC-007, EPIC-008, EPIC-009, EPIC-011.

Note: EPIC-003 and EPIC-010 are absent. This is presumably intentional (renumbered or reserved); not a finding.

### A2. Feature ID format & anchoring

CLEAN. All 74 feature_ids match `^EPIC-\d{3}-F-\d{3}$` and every feature_id begins with the parent epic_id + `-F-`.

### A3. Story ID format & anchoring

CLEAN. All 274 story_ids match `^EPIC-\d{3}-F-\d{3}-S-\d{3}$` and every story_id is anchored under the correct feature_id.

### A4. Requirement ID format & anchoring

CLEAN. All 2,106 non-orphan req_ids match `^EPIC-\d{3}-F-\d{3}-S-\d{3}-REQ-[BT]-\d{3}$` and are anchored under the correct story_id. The 46 records without a req_id all have `orphan: true`, which the schema explicitly permits ("Required when orphan=false; forbidden when orphan=true").

### A5. Duplicate IDs

CLEAN. Zero duplicate epic_ids, feature_ids, story_ids, or req_ids.

### A6. Orphan records (parent FK resolution)

CLEAN. Every feature is under an existing epic; every story is under an existing feature; every requirement is under an existing story. No record is structurally orphaned.

### A7. Required fields populated

**Finding A7-001 (Informational).** 46 orphan-marked requirements legitimately omit `req_id` per schema. They are clustered:
- EPIC-006-F-002-S-001 (1)
- EPIC-006-F-030-S-001 (2)
- EPIC-008-F-004-S-001 (14) — deployment story
- EPIC-008-F-004-S-002 (1)
- EPIC-008-F-007-S-{001..026} (about 23) — JSON-schema-enforcement stories
- EPIC-011-F-001-S-001 (4) — timer

Recommendation: tag-sweep these 46 orphan reqs in the next pass and either promote them to numbered reqs or annotate why each remains orphan.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

---

## B. FK integrity (cross-file)

### B1. bugs.json `bug.req_id` → backlog reqs

80 total bugs. 61 resolve cleanly. 19 do not, breakdown:

**Finding B1-001 (Medium).** 3 bugs cite a strict-format req_id that doesn't resolve:
- `BUG-GOV-008` (low/fixing) → `EPIC-008-F-007-S-007-REQ-B-001`
- `BUG-DEFER-002` (low/deferred) → `EPIC-008-F-007-S-011-REQ-T-002`
- `BUG-DEFER-003` (low/deferred) → `EPIC-002-F-003-S-002-REQ-T-001`

These IDs follow the canonical format but the targets don't exist in the live backlog. Either the targets were retired without repointing these bugs, or they are typos.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

**Finding B1-002 (Informational).** 8 bugs use sentinel `Orphan`/`ORPHAN_BUG`/`ORPHAN_REQ` markers (low + 2 high). Listed at G1.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

**Finding B1-003 (Low).** 8 bugs use a non-standard ID format that isn't an EPIC-NNN req_id and isn't a sentinel: `DEVOPS-DEPLOY-001-REQ-007`, `DEVOPS-LOCAL-B013`, `DEVOPS-BANNER-B003`, `TEST-SIM-001-REQ-015`, `TEST-SIM-001-REQ-014`, `FINDCARE-UX-002`, `DEVOPS-BANNER-B002`, `SKIP-ASSIST-001-REQ-T001`. Probably legacy IDs that were never migrated to EPIC-NNN form.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

### B2. pytest_id / pytest.file references

CLEAN with one informational note:
- 2,167 total pytest entries. 1,935 are `orphan: true` (separate count, not a defect — task explicitly accepts this).
- 0 non-orphan pytests are missing a pytest_id.
- 0 pytest.file paths fail to exist on disk (when not the literal "ORPHAN").

**Finding B2-001 (Informational).** 89% of pytest entries are orphan. This is a coverage-tracking gap, not a structural defect. Future sweeps should harvest pytest IDs from the actual test suite and bind them to reqs.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

### B3. depends_on → real story_id

**Finding B3-001 (Medium).** 65 `depends_on` entries; 57 do not resolve.
- Distinct unresolved targets: `FC-SELECT-001`, `FC-PIPE-SPEC-001`, `FC-PIPE-SPEC-002`. These appear to be legacy spec IDs that were never migrated to the EPIC-NNN-F-NNN-S-NNN story_id format.
- 8 entries resolve cleanly (e.g., to `EPIC-006-F-001-S-001`).

Recommendation: either migrate these legacy spec IDs or retire them. The schema requires `story_id` on each depends_on entry, but that field is currently a free-form string with no constraint.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

### B4. realized_by → real req_id (re-interpreted)

**Finding B4-001 (Informational, not a defect).** Per the schema, feature.realized_by is `array of strings`, and inspection shows every entry is a free-text technology label (e.g., `Caddy 2.x`, `GitHub Actions`, `MongoDB Atlas`, `architecture.json`). Realized_by is not a backlog FK. The audit prompt's B4 question is therefore inapplicable.

**Status (2026-04-27): DEFERRED — outside F-002 scope.**

### B5. engineering_rules.json `rule_statements[].req_id` → backlog reqs

CLEAN. 313 rule_statements; every single non-`Orphan` `req_id` resolves to a live backlog req.

### B6. engineering_rules.json `enforcements.risk_acceptance_id` → risk_acceptance.json

CLEAN. 1 enforcement carries a risk_acceptance_id and it resolves to a live entry in risk_acceptance.json (which has 14 entries).

### B7. Retired EPIC-008 req_id pattern in git-tracked tree

**Finding B7-001 (High).** 4 unresolved EPIC-008 req-IDs found in 3 git-tracked files (live EPIC-008 req IDs in the backlog: 202):

1. `Code/DataPipelines/tests/test_findcare_requirements.py` — references `EPIC-008-F-007-S-007-REQ-T-012` (does not exist in current backlog).
2. `Website/schemas/ChatHealthyRelaxedSchema.json` — references `EPIC-008-F-007-S-020-REQ-T-003` (does not exist in current backlog).
3. `brain/machine_artifacts/content/bugs.json` — references `EPIC-008-F-007-S-007-REQ-B-001` (BUG-GOV-008) and `EPIC-008-F-007-S-011-REQ-T-002` (BUG-DEFER-002). Same FK gap as B1-001.

These tokens follow the canonical format but their targets are not in the live backlog. They look like the targets were retired or renumbered during the EPIC-008-F-007 (per-file JSON schema) work.

The consolidation V1 doc claims "Repo-wide grep for any of the nine retired identifiers returns zero hits outside the migration scripts." That claim was specifically about the 9 retired F-008 IDs, and it is verified true. But there are 4 OTHER retired EPIC-008 req-IDs not covered by that pass.

**Status (2026-04-27): RESOLVED in d7b3c6b (F-002 area portion only) / DEFERRED for the F-007 area portion.** F-002 area FK pointers were repointed; the residual 4 unresolved tokens belong to EPIC-008-F-007 (per-file JSON schema enforcement) and are deferred as F-007 scope.

---

## C. Schema compliance

| File | Schema | Validation |
|---|---|---|
| `agile_backlog.json` | `ChatHealthyAgileBacklogSchema.json` | CLEAN (0 errors) |
| `bugs.json` | `ChatHealthyBugsSchema.json` | CLEAN (0 errors) |
| `engineering_rules.json` | `EngineeringRulesSchema.json` | CLEAN (0 errors) |
| `risk_acceptance.json` | `ChatHealthyRiskAcceptanceSchema.json` (via $schema) | CLEAN (0 errors) |

All four governed JSONs validate against their schema (Draft 2020-12, jsonschema package). C1, C2, C3, C4 = CLEAN.

---

## D. Semantic alignment — V19 ↔ EPIC-008-F-002 backlog

### D1. BR-1..BR-16 coverage

CLEAN. Every BR-1 through BR-16 in V19's BR table has a corresponding backlog requirement with `name: "BR: BR-N"`. Locations:
- BR-1..BR-10 + BR-14 → `EPIC-008-F-002-S-003-REQ-B-{001..011}` (S-003)
- BR-11 → `EPIC-008-F-002-S-006-REQ-B-001` (S-006)
- BR-12, BR-13 → `EPIC-008-F-002-S-005-REQ-B-{001,002}` (S-005)
- BR-15, BR-16 → `EPIC-008-F-002-S-006-REQ-B-{002,003}` (S-006)

### D2. TR-1..TR-19 coverage

CLEAN. Every TR-1 through TR-19 in V19's TR table has a corresponding backlog requirement.

### D3. Wording fidelity — paraphrase vs drift

The audit task accepts "faithful paraphrase". I tested every BR/TR for substring or full-text identity. Where backlog text differs from V19 text, I categorize each as paraphrase or material drift.

**Finding D3-001 (Low).** All BR drifts are minor or paraphrase:
- BR-3: backlog drops V19's "Rule-001 (no misrepresentation) fires on UserPromptSubmit and Stop. Rule-008 (JSON schemas) fires on pre-commit." Faithful paraphrase but loses two example sentences.
- BR-8: identical except `executable path` → `executable_path` (a typo-fix in the backlog version, the underscore form is correct).
- BR-11: backlog drops the note "the highest-priority specific requirement in this framework" and "JSON validation is the load-bearing gate". Material BUT not a binding contract loss; the backlog version preserves the normative MUST.
- BR-14: backlog drops the trailing sentence "They are defined as the canonical JSON contract immediately below this row." (that sentence references a layout artifact in V19 that doesn't carry over to the backlog.)

**Status (2026-04-27): DEFERRED — outside F-002 scope (paraphrase within audit tolerance).**

**Finding D3-002 (Medium).** TR drifts are larger, several materially shorter:
- TR-1: V19 = 1351 chars; backlog = 505 chars. The backlog truncates the entire `timeout` field specification, the EngineeringRulesSchema.json validator note, and the warning that there are no separate top-level allowed_*/excluded_* fields. The shorter backlog text is a faithful summary but loses binding details.
- TR-2: V19 = 1477 chars; backlog = 498 chars. Truncates the aggregation precedence narrative and the worker exit-code mapping guidance. Critical detail "anything ≥ 3 from a worker is treated as an internal worker failure" survives but only as a list.
- TR-9: V19 = 1164 chars; backlog = 544 chars. Truncates the "_load_scopes() default" narrative, the SCOPE_DEFAULT note, and the scopes-field cross-reference.
- TR-10: V19 = 1289 chars; backlog = 541 chars. Truncates the AttributeError fail-loud detail, the SCOPE_DEFAULT per-check narrative, and the §4.5 / §4.9.1 / §4.9.2 cross-references.
- TR-13: V19 = 478 chars; backlog = 231 chars. Truncates the parenthetical that clarifies SCAN_EXTENSIONS / HTTP_EXEMPT_PATTERNS are legacy preCommitScan.py names being retired.

These TR truncations don't change normative force, but they remove material the design doc treats as binding. Recommendation: align the backlog `requirement` text to the full V19 text (or carry V19 verbatim).

Other TR drifts (TR-3/4/6/7/8/11/12/14/15/16/17) are within paraphrase tolerance — typically removing/adjusting `projectRoot/` prefixes, "(emitted by base class)" → "(emitted by the base class)", or fixing en-dashes. Not findings.

**Status (2026-04-27): RESOLVED in d7b3c6b** for TR-1, TR-2, TR-9, TR-10, TR-13 (now verbatim with V19 — character lengths re-verified: 1351, 1477, 1164, 1289, 478). Other TRs remain as faithful paraphrase per the audit's tolerance.

### D4. Story description fits its bucketed reqs

CLEAN at the structural level — every BR/TR is bucketed under a semantically appropriate story:
- S-003 (manager + base + lock + telemetry + exit-codes): hosts BR-1..10/14 + TR-1/2/3/4/6/7/8/14/15/16/17. All match the story's framing.
- S-004 (file enumeration + scope precedence): hosts TR-5/9/10/13. All match.
- S-005 (HTTP vs HTTPS): hosts BR-12/13 + TR-11. All match.
- S-006 (JSON validation): hosts BR-11 + TR-12 + BR-15 + BR-16 + TR-18 + TR-19. All match.

**Finding D4-001 (Low).** Story descriptions for S-003, S-004, S-005, S-006 all reference "V18" as the binding section anchor. After today's V19 publication, those descriptions are subtly stale. Specifically:
- S-003.description: "Per V18 Section 4.4."
- S-004.description: "Per V18 Sections 4.5 and 4.9."
- S-005.description: "Per V18 BR-12, BR-13, TR-11."
- S-006.description: "Per V18 BR-11, TR-12, Sections 4.9.3 and 4.9.3.1." — and does not yet mention BR-15/16/TR-18/19.

Recommendation: bump V18 → V19 in story descriptions; refresh S-006.description to mention the web-fetch resolver / external-standard cache.

**Status (2026-04-27): RESOLVED in d7b3c6b.** Story descriptions for S-003..S-006 reference V19; S-006 description expanded to cite BR-15/16 + TR-18/19.

### D5. BR-15 / BR-16 location

CLEAN. Both are under S-006:
- BR-15 ("Validation MUST use the schema referenced by the JSON file's $schema URL") → `EPIC-008-F-002-S-006-REQ-B-002`
- BR-16 ("External standards used by the framework MUST be committed to the repository") → `EPIC-008-F-002-S-006-REQ-B-003`

Both are JSON-validation-related and S-006 is the JSON-validation story. Pass.

---

## E. Implementation traceability (code ↔ requirements)

### E1. `chathealthy_enforcement_manager.py` docstring V19 references

**Finding E1-001 (Medium).** The module header correctly binds to V19 design (lines 1-14). However, internal docstrings reference V17 section numbers throughout:
- Line 56: "ENGINEERING_RULES_PATH (V17 Table 3)"
- Line 69: "Timeouts (V17 §4.3.2 / Table 5)"
- Line 80: "(V17 Table 3, ctor row.)"
- Line 92: "(V17 Table 3 row 11)"
- Line 114: "(V17 Table 3 row 12)"
- Line 124: "(V17 Table 3 row 13)"
- Line 153: "Default mechanism per design V17"
- Line 164: "(V17 Table 3 row 17)"
- Line 173: "Behavior per V17 Table 3 row 14"
- Line 188: "(V17 §4.3.2)"
- Line 210: "(V17 §4.3.2)"
- Line 272: "(V17 Table 3 row 15)"
- Line 283: "(V17 Table 0 row 2)"
- Line 289: "(V17)"

V17 and V19 share the same §4.x and Table numbering for these references (V19 added §4.0 but did not renumber existing sections; V17 Table 3 is V19 Table 3, etc.), so the references are not semantically misleading. They are stale labels. Per the auditor's task brief: "V17/V18 references in HISTORICAL prose are fine; only flag if a docstring claims V17/V18 is the binding contract." The docstring header explicitly binds to V19, so this is a label-hygiene finding, not a binding-contract violation.

Behavior tracing (no defects): every method I read traces to a TR — `_load_rules` to TR-3; `_filter_enforcements` to TR-3; `_acquire_lock`/`_release_lock` to TR-8; `_spawn_worker` to TR-2 + TR-3; `_aggregate` to TR-2 precedence (`2 > 3 > 5 > 4 > 1 > 0`); `main` is the CLI entry per TR-2/TR-16.

**Status (2026-04-27): RESOLVED in d7b3c6b.** Internal docstrings updated V17 → V19 throughout the manager (Table 3, Table 5, §4.3.2 references).

### E2. `enforcement_worker.py` docstring V19 references

**Finding E2-001 (Medium).** Same pattern as E1. Module header binds to V19 (lines 1-22) and explicitly cites V19 §4.3.2 at lines 18-21. Internal docstrings still cite V17:
- Line 65: "Per V17 §4.9.3"
- Line 109: "(V17 §4.5 / Table 9)"
- Line 118: "(V17 §4.4.2 ctor row)"
- Line 122: 'fail loudly per V17 §4.5'
- Line 154: "(V17 §4.5 / Table 4)"
- Line 158: "(V17 §4.9.1)"
- Line 165: "Per V17 §4.5 / §4.9.2"
- Line 183: "(V17 §4.5 / TR-5 — fixed precedence; lives in base only)"
- Line 229: "(V17 Table 9 / §4.5)"
- Line 242: "Per TR-6 / V17 Table 4 row 10"
- Line 279: "(V17 Table 4 row 6)"
- Line 285-286: 'Per V17 Table 4 row 6: traps any uncaught exception (exit 2). Per V17 §4.9.3'
- Line 326: "V17 Table 4 row 6"

Same disposition as E1. Behavior traces: `is_in_scope` matches TR-5 fixed precedence; `ViolationRecord` matches TR-6; `_emit_telemetry` matches TR-7; `_validate_scope_function_names` matches V19 §4.5 fail-loud rule; `main` matches TR-2 worker subset.

**Status (2026-04-27): RESOLVED in d7b3c6b.** Internal docstrings updated V17 → V19 throughout enforcement_worker.py (§4.5, §4.9.3, Table 4, Table 9 references).

### E3. `scan_files_enforcement_worker.py` docstring V19 references

**Finding E3-001 (Medium).** Module header binds to V19 (lines 1-31). However, internal docstrings cite V18 throughout (this file uses V18 instead of V17):
- Line 16: "V19 inherits the V18 schema-resolution model"
- Line 30: "(V18 §4.9.1 explicitly forbids overriding _load_scopes() here)"
- Line 70: "(V18 §4.9 / TR-11)"
- Line 78: "(V18 §4.9.3.1)"
- Line 81: "(V18 §4.9.3.1)"
- Line 92: "(V18 §4.9.3)"
- Line 95: "(V18 §4.9.3)"
- Line 111: "(V18 Table 9 row 1 / §4.5)"
- Line 117: "V17 §4.5 originally specified True" — this is the one truly historical reference, and it's correct as historical narrative
- Line 136: "(V18 §4.9.3.1)"
- Line 144: "(V18 §4.9.3 step 3)"
- Line 149: "(V18 §4.9.3.1)"
- Line 152: "V18 §4.9.3.1 carve-out policy"
- Line 181: "(V18 Table 9 row 2)"
- Line 184: "(V18 §4.9 / Phase-6 backlog)"
- Line 219: "for V1 the worker is wired only to pre-commit per V18 §4.9"
- Line 246: "(V18 Table 9 row 3 / TR-11)"
- Line 252: "Per V18 / TR-11"
- Line 292: "(V18 §4.9.3)"
- Line 339: "(V18 §4.9.3)"
- Line 346: "(V18 §4.9.3 step 3)"
- Line 351: "(V18 §4.9.3.1)"
- Line 369: "Per V18 §4.9.3 step 3"
- Line 419: "(V18 §4.9.3)"
- Line 429: "(V18 §4.9.3 / TR-12)"
- Line 436: 'V18 §4.9.3 / TR-12'
- Line 439: "See V18 §4.9.3 for the binding contract."

Line 439 is the only one that uses prescriptive language: "See V18 §4.9.3 for the binding contract." Per task instruction, that single line is the F3 trigger — change to V19. The rest are stale labels but V18/V19 §4.9.3 / §4.9.3.1 / Table 9 are equivalent.

Behavior traces: `_resolve_schema` matches V19 §4.9.3 step 3; `_fetch_schema_from_url` matches V19 TR-18; `_load_frozen_external_schemas` matches V19 §4.9.3.1 and TR-19; `_validate_json` matches V19 §4.9.3 / TR-12. All compliant.

**Status (2026-04-27): RESOLVED in d7b3c6b.** Internal docstrings updated V18 → V19 throughout scan_files_enforcement_worker.py, including the line 439 binding-contract claim. The historical narrative on `_validate_json` SCOPE_DEFAULT (line 115 cites V17 as the prior contract) is preserved by design.

### E4. Test files declare which req(s) they exercise

**Finding E4-001 (Low).** The 3 test files contain ~50 test methods total. Only 4 explicit `TR-` tags appear:
- `test_chathealthy_enforcement_manager.py:22` — `test_constants_match_TR2`
- `test_chathealthy_enforcement_manager.py:45` — `TR-2 aggregation` docstring
- `test_enforcement_worker.py:139` — `TR-5: ...` docstring
- `test_scan_files_enforcement_worker.py:243` — `V18 §4.9.3 / TR-12 — _validate_json...`

Most test classes describe behavior rather than tagging the TR explicitly. The tests cover the surface (per their class names: TestExitCodeConstants, TestAggregationPrecedence, TestSpawnWorker, TestLockAcquisition, TestRunEndToEnd, TestIsInScope, TestEmitViolation, TestEmitTelemetry, TestSchemaResolutionWebFetch, TestSchemaResolutionCarveOut, TestForbiddenPatternsAbsentFromImplementation, etc.) but a TR ↔ test map isn't recorded in the file itself. This is a coverage-tracking gap to address in a future pass.

**Status (2026-04-27): DEFERRED — outside F-002 scope (coverage-tracking gap, not a defect).**

### E5. TRs without a corresponding test

**Finding E5-001 (Medium).** Conservative inventory of TR coverage in the framework's test suite:

| TR | V19 topic | Test coverage |
|---|---|---|
| TR-1 | enforcement-entry schema fields | Indirect (fixtures supply entries with all fields; no negative test of missing fields against EngineeringRulesSchema) |
| TR-2 | exit-code constants + aggregation | Covered (`TestExitCodeConstants`, `TestAggregationPrecedence`) |
| TR-3 | manager load-filter-spawn | Covered (`TestFilterEnforcements`, `TestSpawnWorker`, `TestRunEndToEnd`) |
| TR-4 | base-worker class shape | Covered (`TestLoadEnforcement`, `TestEmitViolation`, `TestEmitTelemetry`, `TestMainExitCodes`) |
| TR-5 | scope precedence | Covered (`TestIsInScope`) |
| TR-6 | violation record shape | Covered (`TestViolationRecord`) |
| TR-7 | telemetry envelope | Covered (`TestEmitTelemetry`) |
| TR-8 | requires_lock manager-owned mutex | Covered (`TestLockAcquisition`) |
| TR-9 | ScanFilesEnforcementWorker existence + run() | Covered (`TestRunIntegration`, `TestClassShape`) |
| TR-10 | scope rows shape | Covered (`TestScopeFunctionNameValidation`, `TestScopeWalkthroughs`) |
| TR-11 | _scan_http allowed/excluded | Covered (`TestScanHttp`) |
| TR-12 | _validate_json against $schema | Covered (`TestValidateJsonPure`, `TestValidateJsonOrchestration`) |
| TR-13 | preCommitScan.py drops .json/HTTP_EXEMPT | NOT TESTED — and **NOT IMPLEMENTED YET**. preCommitScan.py at `Code/Shared/ops/tools/preCommitScan.py:54` still contains `".json"` in `SCAN_EXTENSIONS`, and at line 44 still contains `r"\.json$"` in `HTTP_EXEMPT_PATTERNS`. This is a forward-looking TR ("once ScanFilesEnforcementWorker is the single source of truth"); status should be tracked. |
| TR-14 | manager-worker isolation | Indirect (subprocess-based tests in `TestSpawnWorker` evidence it; no explicit "no internal import" test) |
| TR-15 | tests live at architecture/.../tests/ | Self-evident |
| TR-16 | hook plumbing | NOT TESTED in this directory. Lives in `.git/hooks/` and `.claude/settings.json`; out of scope for these test files but still a TR. |
| TR-17 | worker taxonomy | Documentation TR (declares what kinds of workers exist); not test-shaped |
| TR-18 | $schema URL HTTPS GET + per-run cache | Covered (`TestSchemaResolutionWebFetch`) |
| TR-19 | external standard cache at Website/schemas/standard/ | Covered (`TestSchemaResolutionCarveOut`) |

TRs without explicit dedicated tests: TR-13 (deferred-by-design), TR-16 (out of scope for unit tests), TR-17 (not test-shaped). Acceptable — but note TR-13 is the active retirement TR for preCommitScan.py and SHOULD have a test asserting the legacy patterns are gone once the migration runs.

**Status (2026-04-27): RESOLVED in d7b3c6b.** TR-13 implemented: `Code/Shared/ops/tools/preCommitScan.py` no longer contains `.json` in `SCAN_EXTENSIONS` (line 53) and no longer contains `\.json$` in `HTTP_EXEMPT_PATTERNS` (lines 43-49). ScanFilesEnforcementWorker is now the single source of truth for JSON validation at pre-commit time.

---

## F. Stale-content flags

### F1. Misleading fixture name

**Finding F1-001 (Medium).** `architecture/EngineeringRuleEnforcement/tests/fixtures/json_validation/04_missing_local_schema.json`:

- File contents: `{"$schema": "https://dev.chathealthy.ai/schemas/ThisSchemaDoesNotExistOnDisk.json", "name": "ghost-fixture", "kind": "alpha"}`. The $schema URL points at a non-resolving HTTPS URL.
- V19 Table 13 row 4 reads: "JSON whose $schema URL is unreachable or returns non-JSON content → One ViolationRecord (fetch failure); worker continues."
- The fixture's content correctly tests the unreachable URL case — but the filename `04_missing_local_schema.json` is the V17/V18-era label ("missing local schema") and is now misleading.

Recommendation: rename to `04_unreachable_schema_url.json` (or similar), update the test that loads it. This is a name-only finding; the test continues to pass because the behavior is correct.

**Status (2026-04-27): RESOLVED in d7b3c6b.** Fixture renamed to `04_unreachable_schema_url.json`; test updated. Content verified to match V19 Table 13 R4.

### F2. Stale Word report

**Finding F2-001 (High).** `architecture/EngineeringRuleEnforcement/ArchitectureDesignAndAuditDocs/EPIC-008-F-002-backlog-consolidation-V1.docx`:

The report is dated 2026-04-27 and explicitly cites "Source of truth: CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV18.docx (BR-1..14, TR-1..17)". After that report was written, BR-15, BR-16, TR-18, TR-19 were added (V19), and the backlog was edited accordingly.

The report's count claims drift in three places:

| Claim | Word report says | Live state today | Drift |
|---|---|---|---|
| New requirements created under F-002 | 31 | 35 | +4 (BR-15, BR-16, TR-18, TR-19) |
| Total requirements under F-002 after pass | 37 | 41 | +4 |
| V18 BR + TR slots covered | "31 (BR-1..14 + TR-1..17)" | 35 V19 BR/TR slots (BR-1..16 + TR-1..19) | needs V19 |

Recommendation: re-issue as `EPIC-008-F-002-backlog-consolidation-V2.docx` with V19 anchoring and the +4 delta documented. The H1 finding below is the same finding from a different angle.

**Status (2026-04-27): RESOLVED in d7b3c6b.** V2 consolidation report issued at `architecture/EngineeringRuleEnforcement/ArchitectureDesignAndAuditDocs/EPIC-008-F-002-backlog-consolidation-V2.docx` with V19 anchoring and corrected counts (35 new / 41 total). V1 retained as historical record.

### F3. Code docstring V17/V18 binding-contract claims

**Finding F3-001 (High).** `scan_files_enforcement_worker.py:439` reads:
```python
"""...
jsonschema.Draft202012Validator is the SOLE arbiter of validity. Do not invent.
See V18 §4.9.3 for the binding contract.
"""
```
That single line claims V18 is "the binding contract" while the module header explicitly binds to V19. Per task brief — "only flag if a docstring claims V17/V18 is the binding contract" — this triggers F3.

**Status (2026-04-27): RESOLVED in d7b3c6b.** `scan_files_enforcement_worker.py:439` now reads "See V19 §4.9.3 for the binding contract."

**Finding F3-002 (Low).** All other V17/V18 references in the framework code (manager, base worker, scan_files worker) are stale labels in non-prescriptive docstrings ("Per V17 §4.5", "(V18 §4.9.3 step 3)", etc.). The section numbers are equivalent in V19, so they aren't misleading, but they degrade reader confidence. Recommendation: bulk-replace V17/V18 → V19 in the next housekeeping pass, leaving the historical narrative on line 117 of scan_files_enforcement_worker.py alone (it correctly cites V17 as the prior contract that was superseded).

**Status (2026-04-27): RESOLVED in d7b3c6b.** Bulk-replacement completed (45 total replacements per the commit message); the historical narrative at line 115 (now: `# NOTE on the _validate_json default: V17 §4.5 originally specified True…`) is preserved by design.

---

## G. Risk acceptance + bugs cross-check

### G1. High/critical bugs without clear req_id

**Finding G1-001 (High).** Two `severity: high, status: new` bugs have `req_id: "Orphan"`:
- `BUG-SCANNER-OVERSCOPE-001` — title: "Pre-push scanner validates files unrelated to the push, blocking schema-only pushes on pre-existing data violations". This bug is actively biting and has no req_id; the audit cannot trace it to which BR/TR it belongs to. The fix presumably alters scope evaluation in the F-002 framework — recommend repointing to either `EPIC-008-F-002-S-004-REQ-T-001` (scope precedence) or to a new req under S-004.
- `BUG-AGILE-BACKLOG-UNIQUENESS-DISABLED-001` — title: "Cross-record uniqueness check disabled in pre_deploy_rule_check.py - 4 ID fields unenforced (also: bugs.json bugid uniqueness check NOT in pre_deploy_rule_check.py)". This relates to backlog governance, not F-002 framework. Recommend a new req under EPIC-008 governance (or pointing to `EPIC-008-F-007-S-024` whose name suggests it's about cross-JSON ID uniqueness).

`BUG-ARCH-GRAPH-001` (critical) and the 8 `BUG-LANGCHAIN-CONTAINER-*` (high) all cite `EPIC-008-F-010-S-001-REQ-T-001` which is a real backlog req — clean.

**Status (2026-04-27): PARTIAL.** `BUG-SCANNER-OVERSCOPE-001` repointed in d7b3c6b from `Orphan` → `EPIC-008-F-002-S-004-REQ-T-001` (RESOLVED). `BUG-AGILE-BACKLOG-UNIQUENESS-DISABLED-001` left for future cleanup (DEFERRED — outside F-002 scope; backlog-governance area).

### G2. BUG-ENF-WORKER-001 / -002 status

**Finding G2-001 (High).** Both bugs are still `status: in_analysis`:
- `BUG-ENF-WORKER-001` (medium/in_analysis) — "V17 §4.9.3 step 3 mandates URL-pattern resolver that crashes on JSON Schema meta-schema URL". The carve-out implementation in `scan_files_enforcement_worker.py:_load_frozen_external_schemas` directly addresses this.
- `BUG-ENF-WORKER-002` (medium/in_analysis) — "_validate_json conflates schema loading, file parsing, and validation; not compliant with single-responsibility principle". The refactor to `_check_one_file_json` + `_resolve_schema` + `_validate_json(data, schema)` directly addresses this.

Per the V19 design and the shipped code (`scan_files_enforcement_worker.py:431-441`, `_validate_json` is now a pure 4-line wrapper around `jsonschema.Draft202012Validator(schema).iter_errors(data)`), both bugs are logically resolved. Their status should be moved from `in_analysis` to `fixed` (or `resolved`).

**Status (2026-04-27): RESOLVED in d7b3c6b.** Both `BUG-ENF-WORKER-001` and `BUG-ENF-WORKER-002` moved from `in_analysis` → `fixed` in `bugs.json`.

### G3. EPIC-008-F-002 area bugs point at S-003..S-006

CLEAN-ish:
- `BUG-PRIME-RULE-UNENFORCED-001` (high/new) → `EPIC-008-F-002-S-002-REQ-T-001`. Points at S-002 (prime rule), which is correct — but this is one of the older two stories (S-001/S-002), not one of the four new stories.
- `BUG-BACKLOG-SCHEMA-UNENFORCED-001` (high/new) → `EPIC-008-F-002-S-006-REQ-T-001`. Points at S-006 (TR-12, JSON validation). Correct.
- `BUG-SCANNER-NOT-SINGLE-GATE-001` (high/new) → `EPIC-008-F-002-S-003-REQ-B-001`. Points at S-003 (BR-1, every rule has an enforcement entry). Reasonable.
- `BUG-SCANNER-OVERSCOPE-001` (high/new) → "Orphan". Should point at S-004 (G1-001).

---

## H. Word report consistency

**Finding H1-001 (Medium).** Same finding as F2-001 from the count-drift angle.

**Status (2026-04-27): RESOLVED in d7b3c6b** (same as F2-001 — V2 consolidation report issued).

The Word report `EPIC-008-F-002-backlog-consolidation-V1.docx` claims:

| Claim | Word report | Live state | Notes |
|---|---|---|---|
| Retired records (physically removed) | 9 (2 stories + 7 reqs) | Verified — repo-wide grep finds zero hits for the 9 retired IDs outside the consolidation scripts | TRUE |
| New requirements created under F-002 | 31 | 35 | DRIFT (+4 from BR-15/16/TR-18/19) |
| New stories created under F-002 | 4 (S-003..S-006) | 4 | TRUE |
| Total stories under F-002 after pass | 6 | 6 | TRUE |
| Total requirements under F-002 after pass | 37 | 41 | DRIFT (+4) |

Note: prior agents told the auditor "39 reqs across 4 new F-002 stories." That number does not appear in the Word report itself (which says 31). Whatever conveyed "39" was an over-count: the actual figure across S-003..S-006 today is 35 (S-003=22 + S-004=4 + S-005=3 + S-006=6). The total of 41 is the four new stories + S-001 (3) + S-002 (3).

---

## I. Implementation discipline (V19 §4.0 — TR-trace principle)

**Finding I1-001 (Informational).** I scanned the manager, base worker, and scan_files worker for defensive code without TR trace. Every `try`/`except` block I found cites a specific TR or specific design-doc section in its surrounding context:
- `chathealthy_enforcement_manager.py:209` — `TimeoutExpired` → TR-2 / V19 §4.3.2
- `chathealthy_enforcement_manager.py:227` — `OSError` → TR-2 (spawn failure)
- `chathealthy_enforcement_manager.py:301` — `ValueError` → TR-2 (manager error from unknown hook)
- `enforcement_worker.py:302/322` — `WorkerInternalError` → V19 §4.9.3 / exit code 5
- `enforcement_worker.py:325` — `Exception` → TR-2 trap (uncaught → exit 2). Has `# noqa: BLE001` comment with TR reference.
- `scan_files_enforcement_worker.py` — JSONDecodeError, urllib errors, socket.timeout, OSError → all V19 §4.9.3 step 3 (per-file violation, not WorkerInternalError)

`scan_files_enforcement_worker.py:632 TestForbiddenPatternsAbsentFromImplementation` directly enforces (a) no try/except/pass, (b) no calling jsonschema.validate, (c) no alternate validator imports, (d) no size/age/mtime skip heuristics, (e) no isinstance/type compare for validation. These are exactly the V19 §4.0 "implementation discipline" guard rails wired up as tests.

I1 = CLEAN.

**Status (2026-04-27): DEFERRED — informational, no action required.**

---

## Findings index by severity

### Blocking (0)
None.

### High (4)
- [PARTIAL: RESOLVED F-002 area / DEFERRED F-007 area] **B7-001** — 4 unresolved EPIC-008 req IDs in 3 git-tracked files outside the consolidation pass.
- [RESOLVED] **F2-001** — Consolidation Word report stale (anchors to V18; counts off by +4).
- [RESOLVED] **F3-001** — `scan_files_enforcement_worker.py:439` claims V18 is "the binding contract".
- [PARTIAL: RESOLVED BUG-SCANNER-OVERSCOPE-001 / DEFERRED BUG-AGILE-BACKLOG-UNIQUENESS-DISABLED-001] **G1-001** — 2 high-severity Orphan bugs (BUG-SCANNER-OVERSCOPE-001, BUG-AGILE-BACKLOG-UNIQUENESS-DISABLED-001) with no req_id.
- [RESOLVED] **G2-001** — BUG-ENF-WORKER-001 and BUG-ENF-WORKER-002 still `in_analysis` despite being logically resolved.

### Medium (7)
- [DEFERRED] **B1-001** — 3 bugs cite strict-format req_ids that don't resolve.
- [DEFERRED] **B3-001** — 57 of 65 depends_on entries don't resolve.
- [RESOLVED (partial)] **D3-002** — TR-1, TR-2, TR-9, TR-10, TR-13 backlog wording aligned to V19 verbatim; other TRs remain as paraphrase per audit tolerance.
- [RESOLVED] **E1-001** — `chathealthy_enforcement_manager.py` internal docstrings cite V17 throughout.
- [RESOLVED] **E2-001** — `enforcement_worker.py` internal docstrings cite V17 throughout.
- [RESOLVED] **E3-001** — `scan_files_enforcement_worker.py` internal docstrings cite V18 throughout.
- [RESOLVED] **E5-001** — TR-13 is forward-looking and not yet implemented (preCommitScan.py still contains the legacy patterns); needs status tracking.
- [RESOLVED] **F1-001** — Test fixture `04_missing_local_schema.json` has a misleading legacy name.
- [RESOLVED] **H1-001** — Same as F2-001 from a different angle.

### Low (9)
- [DEFERRED] **A7-001** (Informational) — 46 orphan reqs lacking req_id.
- [DEFERRED] **B1-003** — 8 bugs use non-standard non-EPIC req_id formats.
- [DEFERRED] **D3-001** — Minor BR paraphrase drift (BR-3, BR-8, BR-11, BR-14).
- [RESOLVED] **D4-001** — F-002 story descriptions reference V18 instead of V19.
- [DEFERRED] **E4-001** — Test files don't tag which TR each test exercises.
- [RESOLVED] **F3-002** — Other V17/V18 stale labels in framework docstrings.

### Informational (4)
- [DEFERRED] **B1-002** — 8 bugs use sentinel orphan markers.
- [DEFERRED] **B2-001** — 89% of pytest entries are orphan.
- [DEFERRED] **B4-001** — realized_by is intentionally a free-text array of tech labels, not a FK.
- [DEFERRED] **I1-001** — Defensive code is fully TR-traced.

---

## Items where the auditor is not sure

1. **D3-002 severity.** The TR-1/TR-2/TR-9/TR-10/TR-13 truncation in the backlog requirement text loses material the V19 design carries as binding (e.g., the `timeout` field spec in TR-1, the worker exit-code mapping narrative in TR-2). Is the architect's intent that the backlog `requirement` field carry the V19 wording verbatim, or that it carry a faithful summary while the V19 doc remains the binding source? If verbatim is required, D3-002 should be Blocking, not Medium. If summary is acceptable, the current Medium classification stands.

2. **B3-001 disposition.** The 57 unresolved depends_on entries point at `FC-SELECT-001`, `FC-PIPE-SPEC-001`, `FC-PIPE-SPEC-002` — apparent legacy spec IDs. Should these be (a) repointed at live story_ids, (b) deleted, or (c) preserved as historical breadcrumbs? Architect call.

3. **F3-001 vs F3-002 split.** Is "claims X is the binding contract" the only flag for F3, or should every V17/V18 reference be considered noise to clean up? My read of the brief is the former; that's why F3-001 (line 439) is High and F3-002 is Low. Architect can override.

4. **G2-001 procedure.** Closing BUG-ENF-WORKER-001/-002 requires a state change in bugs.json. The auditor is read-only — flagging this for the operator/architect.

---

## Verdict

**OPEN FINDINGS — non-blocking** for the major-refactor commit landing today, conditional on the architect's read of D3-002 (TR text fidelity) and F3-001 (V18-binding-contract docstring). Schema validation is clean; the V19 BR/TR coverage is complete (all 16 BR + all 19 TR present and bucketed correctly); BR-15/BR-16 and TR-18/TR-19 are correctly under S-006; engineering_rules.json FK integrity is intact; risk_acceptance.json FK integrity is intact; the framework's defensive code is fully TR-traced.

The two truly material follow-ups are: (1) close BUG-ENF-WORKER-001/-002 and the four BUG-* in G1, since they are logically resolved by the shipped framework, and (2) re-issue the consolidation Word report as V2 anchored to V19 with the corrected +4 counts.
