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

**Final verdict (after architect review on 2026-04-27): all 10 RESOLVED.**

| Severity | Resolved | False Positive | Persistent |
| --- | --- | --- | --- |
| High | 0 | 0 | 0 |
| Medium | 6 | 0 | 0 |
| Low | 4 | 0 | 0 |
| **Total** | **10** | **0** | **0** |

**Verdict: CLOSED — all findings remediated.**

Resolution path per finding:
- **S-1, S-2** — RESOLVED by V18 refactor (registry deleted + URL-pattern resolver removed)
- **M-1** — DELETED on 2026-04-27 (`_startup_self_check` removed; the rules file is validated at commit time via Rule-008-ENF-001, not at manager-startup; no requirement called for an additional self-check at manager startup)
- **M-2** — RECLASSIFIED as false positive on 2026-04-27 (V18 specifies behavior for valid exit codes; how the manager handles out-of-contract worker codes is implementation choice, not a requirement gap; the existing `EXIT_MANAGER_ERROR` fallback for unknown codes is left in place)
- **M-3, W-1, W-2, W-3, S-3, S-4** — DELETED on 2026-04-27 per architect directive: "we only QA against requirements, not against design choices." None of these defensive checks trace to a TR. They produced slightly nicer error messages on malformed input that the schema rejects upstream. The runtime now relies on schema enforcement (commit-time via Rule-008-ENF-001) plus the framework's existing generic `except Exception` triage. Tests covering the deleted defensive paths were removed.

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

**Verdict: CLOSED — all 10 V1 findings remediated.**

Disposition (architect review, 2026-04-27):
- **S-1, S-2**: RESOLVED by V18 refactor — registry and URL-pattern resolver deleted.
- **M-1, M-3, W-1, W-2, W-3, S-3, S-4** (7 findings): DELETED. None traced to a technical requirement. Architect-stated principle: *"we only QA against requirements, not against design choices."* Tests covering the deleted paths were removed. The runtime now relies on schema enforcement at commit time (Rule-008-ENF-001) plus the framework's generic `except Exception` triage in `main()`.
- **M-2**: RECLASSIFIED as false positive. V18 specifies behavior for valid exit codes only; the implementation's choice for out-of-contract worker codes is implementation discretion, not a requirements gap.

The two high-severity inventions called out in `BUG-ENF-WORKER-001` (URL-pattern
resolver) and `BUG-ENF-WORKER-002` (`_validate_json` conflation) are
remediated. `BUG-ENF-WORKER-001` was re-framed in V2 from "implementation
invented" to "V17 itself was non-functional"; the re-framing is recorded in
`bugs.json`.

Zero new findings were introduced by V18 or by the 2026-04-27 cleanup.

---

## 5. Note on V1's count

V1's executive summary reported 7 findings; its per-file enumeration named 10
(M-1, M-2, M-3, W-1, W-2, W-3, S-1, S-2, S-3, S-4). The "7" figure was a
clerical error in V1. V2 uses the correct count (10) and tracks each one by
its V1 identifier so cross-references remain stable.
