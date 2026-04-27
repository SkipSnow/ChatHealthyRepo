# Implementation-Invention Audit V1

Binding contract: `CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV17.docx`

Scope of audit:
- `architecture/EngineeringRuleEnforcement/code/chathealthy_enforcement_manager.py`
- `architecture/EngineeringRuleEnforcement/code/enforcement_worker.py`
- `architecture/EngineeringRuleEnforcement/code/scan_files_enforcement_worker.py` (post-refactor)

Date: 2026-04-27
Author: Claude (audit run on Skip's directive)

---

## 1. Executive summary

| Severity | Count |
| --- | --- |
| High (blocks operation OR contradicts V17) | 0 |
| Medium (latent invention; defensive logic for cases V17 does not enumerate) | 5 |
| Low (cosmetic / documentation drift) | 2 |
| Total findings | 7 |

Blocking findings: 0. Latent findings: 7. Verdict: **OPEN FINDINGS** (none are blocking; all are latent invention worth Skip's review). The two known high-severity inventions called out in the bug records (`BUG-ENF-WORKER-001` URL resolver invention, `BUG-ENF-WORKER-002` `_validate_json` conflation) have been resolved by the refactor and are no longer findings against the current code; they are recorded against the prior code in `bugs.json`.

The pre-refactor scan_files_enforcement_worker.py contained two high-severity inventions; both are fixed and the post-refactor worker is the file under audit here.

---

## 2. Per-file findings

### 2.1 `chathealthy_enforcement_manager.py`

#### Finding M-1 — `_startup_self_check` is invented

- File:line — `chathealthy_enforcement_manager.py:131-199` (the entire `_startup_self_check` method) and the call site at `:111-113`.
- Invented requirement — Before any dispatch, the manager loads `EngineeringRulesSchema.json`, builds a `Draft202012Validator`, and validates `engineering_rules.json` against it. On any failure (file missing, malformed JSON, schema invalid against Draft 2020-12, content fails validation) it returns `EXIT_MANAGER_ERROR` (2).
- V17 reference — The manager's `run()` row in §4.4.1 stipulates only "Load rules, filter enforcement[] by hook, spawn each worker as a subprocess via `_spawn_worker`, aggregate exit codes by precedence, return worst code." TR-3 likewise. There is no V17 requirement to pre-validate the rules file against its schema before dispatch. The method's own docstring concedes this is a "Skip risk-register #2 (chicken-and-egg)" guard, not a V17 requirement.
- Severity — Medium. It is defensive logic for a failure mode V17 does not enumerate. It is not blocking — the check is correct and helpful in practice. But it adds a code path the contract did not ask for, and changes the manager's `run()` semantics from "dispatch and aggregate" to "validate-then-dispatch-and-aggregate", which is exactly the kind of expansion V17's tight `run()` row exists to prevent.
- Recommendation — Either (a) elevate the self-check into V17 explicitly (i.e. amend the design doc), or (b) move it behind a documented opt-in flag so the V17 `run()` contract stays clean. Until one of those happens, the method is invention against the current contract.

#### Finding M-2 — `_aggregate` returns `EXIT_MANAGER_ERROR` for unknown codes

- File:line — `chathealthy_enforcement_manager.py:387-395` (`_aggregate`, specifically the trailing `return self.EXIT_MANAGER_ERROR`).
- Invented requirement — When the input list contains a code not in `_PRECEDENCE = (2, 3, 5, 4, 1, 0)`, the method returns `EXIT_MANAGER_ERROR` (2).
- V17 reference — §4.4.1 `_aggregate` row: "Apply precedence 2 > 3 > 5 > 4 > 1 > 0; default to 0 if empty." V17 does not stipulate a behavior for unknown codes; the contract assumes worker exit codes are 0/1/2 and that the manager has already promoted ≥3 to 5 (TR-2). Inventing a fall-through to `EXIT_MANAGER_ERROR` for codes outside the precedence list is a failure mode V17 does not describe.
- Severity — Medium. Latent — in practice all worker exit codes have already been promoted by `_spawn_worker` so this branch is unreachable. But the branch implies the aggregator can manufacture a manager-error verdict from an unknown worker code, which is not the V17 model.
- Recommendation — Either delete the unreachable branch and trust that `_spawn_worker` promotes exit codes correctly, or add a contract-level note explaining when this branch can fire. Cleanest: delete it, raise an explicit assertion in development if it ever fires, or fall through to `EXIT_OK` per the "default to 0 if empty" V17 phrasing.

#### Finding M-3 — `_spawn_worker` validates `executable_path` and `timeout` shape

- File:line — `chathealthy_enforcement_manager.py:282-307` (the two `isinstance` / shape checks in `_spawn_worker`).
- Invented requirement — `_spawn_worker` checks that `executable_path` is a non-empty string and that `timeout` is a positive int, returning `EXIT_WORKER_SPAWN_FAILURE` if either is malformed.
- V17 reference — §4.4.1 `_spawn_worker` row: "Read enforcement.executable_path. Resolve the timeout: enforcement entry's optional `timeout` field if present, otherwise the class constant DEFAULT_TIMEOUT_SECONDS." Per §4.6 / Table 5 (and also TR-1), shape correctness of the entry is enforced by `EngineeringRulesSchema.json` at the JSON-validation layer, and `executable_path` is required. V17 does not stipulate a runtime shape recheck inside the manager.
- Severity — Medium. Latent. The schema validator already catches this. The runtime defensive checks are duplication.
- Recommendation — Trust the schema and remove the shape rechecks, OR keep them but anchor them in the design doc as a defense-in-depth measure. Either is fine; the current state is undocumented duplication.

---

### 2.2 `enforcement_worker.py`

#### Finding W-1 — `_validate_scope_function_names` performs row-shape and type-name checks beyond V17 §4.5

- File:line — `enforcement_worker.py:166-193` (`_validate_scope_function_names`).
- Invented requirement — In addition to the `getattr(self, row[0])` resolution V17 §4.5 names (and which surfaces typos as `AttributeError`), the method also rejects malformed rows (`len(row) != 3`) and unknown `scope_list_type` values with `WorkerInternalError`.
- V17 reference — §4.5 states: "the base class resolves function_name to a bound method via `getattr(self, row[0])` at startup; a typo in the method name surfaces at worker startup as an `AttributeError` rather than as a silently dropped rule." §4.6 states the row shape and the four canonical types are enforced by `EngineeringRulesSchema.json`. Recapping those checks in the runtime is duplication.
- Severity — Low. The defensive checks are harmless and provide a clearer error message than the eventual schema-layer failure. But they are not in V17.
- Recommendation — Leave as-is and document that this is defense-in-depth, OR delete and trust the schema. Either is fine.

#### Finding W-2 — `_load_scopes` rejects non-list scopes with `WorkerInternalError`

- File:line — `enforcement_worker.py:152-164` (`_load_scopes`).
- Invented requirement — Type-checks that `scopes` is a list and raises `WorkerInternalError` if not.
- V17 reference — §4.5 / §4.4.2 `_load_scopes` row: "Default implementation returns `self.entry.get('scopes', [])`." That is all V17 says. Type-validity is, again, the schema validator's job.
- Severity — Low. Same character as W-1: defense-in-depth that V17 doesn't require.
- Recommendation — Keep or remove; document either way.

#### Finding W-3 — `main()` traps `(AttributeError, ValueError, KeyError)` and returns `EXIT_WORKER_ERROR`

- File:line — `enforcement_worker.py:318-321` (the second `except` block in `main`).
- Invented requirement — `AttributeError` on the constructor (raised when a scope row's `function_name` is not a method) is intercepted and converted to exit 2 (`EXIT_WORKER_ERROR`).
- V17 reference — §4.5 expressly says a typo in `function_name` "surfaces at worker startup as an `AttributeError` rather than as a silently dropped rule". The intent is loud failure. V17 §4.4.2 `main()` row: "trap any uncaught exception (exit 2)". So returning exit 2 is consistent with V17, but the explicit `(AttributeError, ValueError, KeyError)` triage is invention layered over the generic trap.
- Severity — Medium. The end-state behavior matches V17 (exit 2). The triage is invention but harmless in effect.
- Recommendation — Delete the explicit triage; let the generic `except Exception` on the next handler do the work. The targeted `except` adds a maintenance burden (someone now has to keep `KeyError` etc. in sync with reality) and gives no extra behavior.

---

### 2.3 `scan_files_enforcement_worker.py` (post-refactor)

#### Finding S-1 — Schema registry pre-loaded at `__init__` (architectural departure from V17 §4.9.3 step 4)

- File:line — `scan_files_enforcement_worker.py:105-160` (`_build_schema_registry`) and the call from `__init__` at `:100`.
- Invented requirement — The worker walks `Website/schemas/*.json` once at startup, builds an in-memory `dict[str, dict]` keyed on each schema's `$id`, pre-registers the JSON Schema 2020-12 meta-schema URL against `jsonschema.Draft202012Validator.META_SCHEMA`, and runs `check_schema` on every loaded schema.
- V17 reference — §4.9.3 step 4 says: "Load the schema. `with open(schema_path, encoding='utf-8') as f: schema = json.load(f)`." Step 5 says construct the validator with `Draft202012Validator(schema)` and run `check_schema` at validation time, not at init. The V17 algorithm loads and checks the schema *per file being validated*; the implementation now does both *once at __init__* against the entire `Website/schemas/` tree.
- Severity — Medium. The departure is intentional and resolves `BUG-ENF-WORKER-001` (the URL-pattern resolver invention) and `BUG-ENF-WORKER-002` (`_validate_json` conflation). It also fails fast on broken schemas at startup rather than mid-pre-commit. It is, however, an architectural change V17 §4.9.3 does not stipulate.
- Recommendation — Update V17 to V18 to describe the registry pattern. Until the design doc is amended, the current code is a deliberate, well-bounded departure documented in `bugs.json` (BUG-ENF-WORKER-001, BUG-ENF-WORKER-002). Keep.

#### Finding S-2 — Fallback URL convention `https://dev.chathealthy.ai/schemas/<filename>` for schemas missing `$id`

- File:line — `scan_files_enforcement_worker.py:153-159` (the `else` branch of the `$id` lookup).
- Invented requirement — When a schema file under `Website/schemas/` does not declare `$id`, the registry registers it under `https://dev.chathealthy.ai/schemas/<filename>`.
- V17 reference — V17 Table 12 (§4.9.3) lists exactly this URL convention as the canonical mapping. So the fallback is V17-anchored, not invented; but the fallback only fires if a schema lacks `$id`, which V17 does not contemplate (V17 assumes the convention holds either via `$id` on the schema or via the URL the data file declares).
- Severity — Low. Compliant with V17 Table 12; only "invented" in the sense that V17 does not specifically describe a fallback when `$id` is absent. In practice every shipped schema declares `$id`.
- Recommendation — Keep. Leave a comment that points back to V17 Table 12 (already present).

#### Finding S-3 — `_load_data` raises `WorkerInternalError` on `FileNotFoundError`

- File:line — `scan_files_enforcement_worker.py:330-333` (the `except FileNotFoundError` in `_load_data`).
- Invented requirement — A target file that exists at scope-evaluation time but disappears before parse raises `WorkerInternalError`, which `main()` converts to exit 5.
- V17 reference — §4.9.3 step 1 enumerates only `json.JSONDecodeError`. V17 does not describe a "file vanished mid-run" failure mode.
- Severity — Low. Defensive against a TOCTOU race that is theoretically possible in pre-commit. Will not fire in normal operation.
- Recommendation — Keep, but understand it is invention. Could equally be re-classed as a per-file ViolationRecord rather than a worker crash.

#### Finding S-4 — `_scan_http` swallows `OSError` and prints to stderr

- File:line — `scan_files_enforcement_worker.py:227-234` (the `try/except OSError` around the `open` call).
- Invented requirement — On any `OSError` reading the target file (permission denied, file vanished, etc.), the method returns `[]` (no violation) and writes a stderr line.
- V17 reference — §4.9 / TR-11 do not enumerate an "unreadable file" failure mode for `_scan_http`. The expected behavior on read failure is undefined in V17.
- Severity — Medium. Latent invention. A file that cannot be read is silently treated as compliant. V17 would arguably want either a violation or a worker-internal error.
- Recommendation — Decide explicitly. Either treat read-failure as a per-file ViolationRecord (most defensible — the file might contain `http://` and we don't know) or as a `WorkerInternalError`. Pick one and anchor it in the design doc.

---

## 3. Compliance statement

**Verdict: OPEN FINDINGS.**

Reasoning:
- The two high-severity inventions that were blocking pre-commit (URL-pattern resolver in `_load_schema`, `_validate_json` conflation) have been remediated by the Job-2 refactor. No high-severity finding remains.
- Five medium-severity findings (M-1, M-2, M-3, W-3, S-1, S-4 — counting S-1 as the boundary departure that is technically a contract change worth Skip's signoff) and two low-severity findings (W-1, W-2, S-2, S-3) remain.
- None of the open findings blocks operation. The framework is operationally compliant. The findings are about contract drift: the implementation does more than V17 asks for (mostly defensive) or differs in shape (S-1, the registry).
- Skip's choice: either accept the open findings (and update the V17 design doc to describe the registry, the startup self-check, and the explicit triage paths so they become contract), or trim the implementation back to exactly what V17 specifies.

Recommended next step: produce a V18 design-doc amendment that absorbs the registry pattern (S-1) and the manager startup self-check (M-1), and trim the smaller defensive checks (M-2, M-3, W-1, W-2, W-3) so the code matches V17 line-for-line on those rows.
