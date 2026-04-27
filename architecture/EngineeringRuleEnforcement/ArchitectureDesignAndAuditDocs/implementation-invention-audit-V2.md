# Implementation-Invention Audit V2

Binding contract: `CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV18.docx`

Scope of audit:
- `architecture/EngineeringRuleEnforcement/code/chathealthy_enforcement_manager.py`
- `architecture/EngineeringRuleEnforcement/code/enforcement_worker.py`
- `architecture/EngineeringRuleEnforcement/code/scan_files_enforcement_worker.py` (post web-fetch refactor)

Date: 2026-04-27
Author: Claude (audit re-run on Skip's directive after Option A architectural decision)

---

## 1. Executive summary

V1 listed 10 findings (M-1, M-2, M-3, W-1, W-2, W-3, S-1, S-2, S-3, S-4) in §2 but
its summary table reported "7"; the table count was a clerical error in V1. V2
re-counts honestly: V1 had **10 findings**.

V2 verdict against the new architecture (V18 web-fetch + meta-schema carve-out):

| Severity | Persistent | Resolved | New |
| --- | --- | --- | --- |
| High | 0 | 0 | 0 |
| Medium | 4 | 2 | 0 |
| Low | 4 | 0 | 0 |
| **Total** | **8** | **2** | **0** |

**Verdict: OPEN FINDINGS (non-blocking).** Two V1 findings are RESOLVED by the
V18 refactor (S-1 schema-registry departure; S-2 fallback URL convention — both
became moot when the URL-pattern resolver and the registry-at-init were
deleted). The remaining 8 V1 findings are independent of the schema-resolution
change and persist into V18; none of them blocks operation. No new findings
were introduced by the V18 refactor.

The two known high-severity inventions called out in `bugs.json`
(`BUG-ENF-WORKER-001` URL resolver, `BUG-ENF-WORKER-002` `_validate_json`
conflation) remain resolved; `BUG-ENF-WORKER-001` was re-classified in V2 from
"implementation invented the URL resolver" to "V17 §4.9.3 step 3 itself
mandated a non-functional resolver" — the implementation was honoring V17 as
written. See `bugs.json` for the rewritten record.

---

## 2. Per-file findings

### 2.1 `chathealthy_enforcement_manager.py`

#### Finding M-1 — `_startup_self_check` is invented
**Status: PERSISTENT (Medium).** Unchanged by the V18 refactor.

- File:line — `chathealthy_enforcement_manager.py:131-199`, called from `:111-113`.
- Invented requirement — Manager pre-validates `engineering_rules.json` against `EngineeringRulesSchema.json` on startup; on failure returns `EXIT_MANAGER_ERROR` (2).
- V18 reference — Manager `run()` row in §4.4.1 still says only "load rules, filter by hook, spawn each worker, aggregate." V18 did not absorb the self-check into the contract.
- Recommendation — Either elevate into V19 §4.4.1 explicitly, or move behind a documented opt-in flag. Until then, the method is invention against the current contract.

#### Finding M-2 — `_aggregate` returns `EXIT_MANAGER_ERROR` for unknown codes
**Status: PERSISTENT (Medium).** Unchanged by the V18 refactor.

- File:line — `chathealthy_enforcement_manager.py:387-395`.
- V18 reference — §4.4.1 `_aggregate` row remains "Apply precedence 2 > 3 > 5 > 4 > 1 > 0; default to 0 if empty." V18 still does not stipulate behavior for unknown codes.
- Recommendation — Delete the unreachable branch or document the manufactured-error semantics.

#### Finding M-3 — `_spawn_worker` validates `executable_path` and `timeout` shape
**Status: PERSISTENT (Medium).** Unchanged by the V18 refactor.

- File:line — `chathealthy_enforcement_manager.py:282-307`.
- V18 reference — Shape correctness is still the schema validator's job per §4.6 / Table 5. The runtime defensive checks remain undocumented duplication.
- Recommendation — Trust the schema or anchor the rechecks as documented defense-in-depth.

---

### 2.2 `enforcement_worker.py`

#### Finding W-1 — `_validate_scope_function_names` performs row-shape and type-name checks beyond V18 §4.5
**Status: PERSISTENT (Low).** Unchanged.

- File:line — `enforcement_worker.py:166-193`.
- Recommendation — Document as defense-in-depth or delete and trust the schema.

#### Finding W-2 — `_load_scopes` rejects non-list scopes with `WorkerInternalError`
**Status: PERSISTENT (Low).** Unchanged.

- File:line — `enforcement_worker.py:152-164`.
- Recommendation — Same character as W-1; keep or remove.

#### Finding W-3 — `main()` traps `(AttributeError, ValueError, KeyError)` and returns `EXIT_WORKER_ERROR`
**Status: PERSISTENT (Medium).** Unchanged.

- File:line — `enforcement_worker.py:318-321`.
- Recommendation — Delete the explicit triage and let the generic `except Exception` handler do the work.

---

### 2.3 `scan_files_enforcement_worker.py` (post-V18 refactor)

#### Finding S-1 — Schema registry pre-loaded at `__init__`
**Status: RESOLVED (was Medium).**

- V1 finding — Worker walked `Website/schemas/*.json` once at `__init__`, built an `dict[$id → schema]` registry, ran `check_schema` on every loaded schema. This was an architectural departure from V17 §4.9.3 step 4 (per-file load).
- Resolution — V18 §4.9.3 replaces the registry-at-init pattern with web fetch. The worker no longer walks `Website/schemas/`; `_build_schema_registry` and `_register_schema_file` have been deleted. The only init-time load is the carve-out for the JSON Schema 2020-12 meta-schema, whose URL is contractually frozen per V18 §4.9.3.1. That carve-out is anchored explicitly in V18, so it is no longer invention.

#### Finding S-2 — Fallback URL convention `https://dev.chathealthy.ai/schemas/<filename>` for schemas missing `$id`
**Status: RESOLVED (was Low).**

- V1 finding — The registry registered schemas under a fallback URL constructed from the filename when `$id` was absent.
- Resolution — The registry no longer exists. The worker fetches whatever URL the data file declares in `$schema`; if the URL does not resolve, that is a per-file violation per V18 §4.9.3 step 3. There is no fallback URL convention to invent any more.

#### Finding S-3 — `_load_data` raises `WorkerInternalError` on `FileNotFoundError`
**Status: PERSISTENT (Low).** Unchanged by V18.

- File:line — `scan_files_enforcement_worker.py` (`_load_data` `except FileNotFoundError`).
- V18 reference — §4.9.3 step 1 still enumerates only `json.JSONDecodeError`. The TOCTOU "file vanished" path remains undescribed by V18.
- Recommendation — Decide explicitly: per-file `ViolationRecord` (cheaper, defensible) or `WorkerInternalError` (current). Anchor the choice in the design doc.

#### Finding S-4 — `_scan_http` swallows `OSError` and prints to stderr
**Status: PERSISTENT (Medium).** Unchanged by V18.

- File:line — `scan_files_enforcement_worker.py` (`_scan_http`'s `try/except OSError`).
- V18 reference — §4.9 / TR-11 still do not enumerate an "unreadable file" failure mode for `_scan_http`. The expected behavior on read failure is undefined.
- Recommendation — Decide explicitly: treat read-failure as a per-file `ViolationRecord` (defensible — the file might contain `http://` and we don't know) or as a `WorkerInternalError`. Pick one and anchor it.

---

## 3. Findings introduced by the V18 refactor

None. The web-fetch resolver and the carve-out map are V18 contract — they are
called out explicitly in §4.9.3 step 3, §4.9.3.1, and Table 12. Per-file
ViolationRecords for fetch failures are explicit in V18 §4.9.3 step 3. The
implementation matches the design surface line-for-line.

The small set of mechanical helpers in the worker (`_resolve_schema`,
`_fetch_schema_from_url`, `_violation_for_fetch_failure`) are single-responsibility
splits of the orchestration described in V18 §4.9.3 — they introduce no
behavior beyond what step 3 specifies.

---

## 4. Compliance statement

**Verdict: OPEN FINDINGS (non-blocking).**

Reasoning:
- The two high-severity inventions called out in `BUG-ENF-WORKER-001`
  (URL-pattern resolver) and `BUG-ENF-WORKER-002` (`_validate_json` conflation)
  are remediated. `BUG-ENF-WORKER-001` was re-framed in V2 from
  "implementation invented" to "V17 itself was non-functional"; the
  re-framing is recorded in `bugs.json`.
- Two V1 findings (S-1 registry departure, S-2 fallback URL convention) are
  RESOLVED by the V18 refactor — the registry and the fallback URL are gone.
- Eight V1 findings (M-1, M-2, M-3, W-1, W-2, W-3, S-3, S-4) PERSIST. None of
  them is blocking. They are about contract drift in the manager and the
  base class — independent of the schema-resolution change. They were not
  in scope for the V18 task and are deferred to a future pass.
- Zero new findings were introduced by V18.

Recommended next step: a follow-on pass that either (a) trims the
defensive logic flagged in M-1, M-2, M-3, W-1, W-2, W-3, S-3, S-4 so the
runtime matches V18 line-for-line, or (b) absorbs the defensive logic into
V19 explicitly. Either path closes the eight persistent findings.

---

## 5. Note on V1's count

V1's executive summary reported 7 findings; its per-file enumeration named 10
(M-1, M-2, M-3, W-1, W-2, W-3, S-1, S-2, S-3, S-4). The "7" figure was a
clerical error in V1. V2 uses the correct count (10) and tracks each one by
its V1 identifier so cross-references remain stable.
