"""
EPIC-008-F-002 backlog consolidation.
Source-of-truth: V18 design doc (BR-1..14, TR-1..17).
Inventory: _oneshots/test_output/scan_validate_relevance.json (71 flagged entities).
Outputs: in-place edit of agile_backlog.json, plus mapping table to JSON file.
"""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
BACKLOG = ROOT / "brain" / "machine_artifacts" / "content" / "agile_backlog.json"
FLAGGED_FILE = ROOT / "_oneshots/test_output" / "scan_validate_relevance.json"
MAPPING_OUT = ROOT / "Code" / "_pending" / "backlog_consolidation" / "id_mapping.json"

# ---- 4 new stories under EPIC-008-F-002 ----

NEW_STORIES = [
    {
        "story_id": "EPIC-008-F-002-S-003",
        "name": "Rules infrastructure: manager, base worker, lock, timeout, telemetry, exit-code aggregation",
        "description": "ChatHealthyEnforcementManager + EnforcementWorker (abstract base) deliver the framework: read-only rules-registry load, hook-filtered enforcement dispatch, manager-owned mutex coordination, per-worker subprocess timeout, structured violation/telemetry envelopes, and worst-code-wins aggregation. Per V18 Section 4.4. The manager is invoked by every Git and Claude Code hook with a single hook_name argument.",
        "size": "L",
        "status": "done",
        "approval": "approved",
        "orphan": False,
        "br_trs": [
            ("B", "001", "BR-1",  "Every engineering rule that the team intends to enforce MUST carry at least one programmatic enforcement entry. A rule with an empty enforcement array is documentation, not governance, and SHALL be either populated, retired, or annotated as documentation-only."),
            ("B", "002", "BR-2",  "Enforcement runs at the moment a violation can occur — the relevant Git or Claude Code hook — not on a separate cadence. There is no nightly job that catches yesterday's mistake."),
            ("B", "003", "BR-3",  "An engineering rule MUST be able to attach to any of the four Git hooks (pre-commit, commit-msg, post-commit, pre-push) and any of the six Claude Code hooks (SessionStart, UserPromptSubmit, PreToolUse, Stop, SessionEnd, InstructionsLoaded). The framework is hook-agnostic; the enforcement entry declares its hook."),
            ("B", "004", "BR-4",  "The system MUST be debuggable by a human. One file per role. One class per file. One entry point. Per-enforcement, per-worker structured stdout and telemetry, with worst exit code propagated."),
            ("B", "005", "BR-5",  "The framework MUST handle multi-PID race conditions correctly. The same hook can fire many times concurrently across different Claude Code PIDs and Git invocations. The manager acquires a lock before dispatching any worker whose enforcement entry declares requires_lock = True; in-process locks are forbidden."),
            ("B", "006", "BR-6",  "Adding the next enforcement MUST be a small, additive change. New enforcement entries get a new enforcement_id and — if needed — a new worker subclass; existing rules and workers are not touched."),
            ("B", "007", "BR-7",  "Every worker MUST inherit from EnforcementWorker. Subclasses implement only their domain-specific check logic. The base class owns scope evaluation, violation shape, exit codes, telemetry, and error handling so every worker behaves the same from the manager's perspective."),
            ("B", "008", "BR-8",  "Enforcement entries are first-class records: every entry carries a stable enforcement_id (format <rule_id>-ENF-NNN), a declared hook, an executable_path, a scopes field carrying scope-list rules in the four canonical types (allowed_exact, allowed_pattern, excluded_exact, excluded_pattern), and a requires_lock boolean. They can be cited in audit trails, bug reports, and the backlog like requirements are today."),
            ("B", "009", "BR-9",  "The architecture MUST be aligned with the EPIC / Feature / Story tree. Filesystem mirrors operating model. The home for this feature is projectRoot/architecture/EngineeringRuleEnforcement/. crossEpicConcerns/ holds shared business artifacts only — no code lives there."),
            ("B", "010", "BR-10", "Once V1 ships, BR-1 through BR-9 are demonstrably satisfied for the worked example (file scanning on pre-commit) and the framework is in place to absorb every other rule on its own schedule."),
            ("B", "011", "BR-14", "The framework MUST support all four scope-list types on every enforcement entry, even when a given worker uses only a subset. The four permitted terms (allowed_exact, allowed_pattern, excluded_exact, excluded_pattern) are the only vocabulary used anywhere in this system."),
            ("T", "001", "TR-1",  "engineering_rules.json schema MUST require these per-enforcement fields: enforcement_id (string, format <rule_id>-ENF-NNN); hook (enum of the ten Git+Claude Code hook names); executable_path (string, repo-relative path to the worker script); requires_lock (boolean); scopes (array of three-column rows [function_name, scope_list_type, list_of_terms] where scope_list_type is one of the four canonical types); timeout (integer, optional, seconds; manager default DEFAULT_TIMEOUT_SECONDS = 30 when omitted)."),
            ("T", "002", "TR-2",  "ChatHealthyEnforcementManager lives at architecture/EngineeringRuleEnforcement/code/chathealthy_enforcement_manager.py. Single class, single main(). Constructor __init__(hook_name: str). run() returns one of EXIT_OK = 0, EXIT_VIOLATIONS_FOUND = 1, EXIT_MANAGER_ERROR = 2, EXIT_WORKER_SPAWN_FAILURE = 3, EXIT_WORKER_TIMEOUT = 4, EXIT_WORKER_INTERNAL_ERROR = 5. Aggregation precedence (highest wins): 2 > 3 > 5 > 4 > 1 > 0. Worker exits >= 3 are promoted by the manager to EXIT_WORKER_INTERNAL_ERROR."),
            ("T", "003", "TR-3",  "Manager loads engineering_rules.json read-only, filters enforcement[] by hook, and for each matching enforcement reads executable_path and spawns the worker via subprocess.run([sys.executable, executable_path, enforcement_id], timeout=...). The manager collects each subprocess returncode and aggregates by the TR-2 exit-code precedence."),
            ("T", "004", "TR-4",  "EnforcementWorker (abstract base) lives at architecture/EngineeringRuleEnforcement/code/enforcement_worker.py. It owns: __init__(enforcement_id), _load_enforcement(), is_in_scope(resource), main() (CLI entry that parses enforcement_id, instantiates the subclass, calls run(), returns its exit code), violation emission, telemetry emission, and structured error handling. Subclasses implement run() and any private helper methods their domain requires."),
            ("T", "005", "TR-6",  "Violation record shape (emitted by the base class): {enforcement_id, rule_id, resource, message, severity}. severity in {info, warn, error}."),
            ("T", "006", "TR-7",  "Telemetry envelope (emitted by the base class): start record and end record per worker invocation, both carrying enforcement_id, hook, duration_ms, files_scanned, violation_count, exit_code. Format: one JSON object per line to stdout."),
            ("T", "007", "TR-8",  "When an enforcement entry declares requires_lock = True, the manager MUST acquire a process-safe lock keyed on enforcement_id before invoking subprocess.run, and MUST release the lock after subprocess.run returns (clean, non-zero, or timeout). Default mechanism: advisory file lock. In-process Python locks are forbidden. Workers do not declare a concurrency primitive."),
            ("T", "008", "TR-14", "Manager and workers MUST NOT import each other's internals. The only contract is enforcement_id (str) in via CLI argv, exit code (int) out via subprocess returncode, and the structured stdout (violation + telemetry) envelope."),
            ("T", "009", "TR-15", "Tests live at architecture/EngineeringRuleEnforcement/tests/. The base class ships with its own test suite covering scope evaluation, exit-code semantics, violation shape, telemetry envelope, and the try/except fail-safe. Each new worker ships with at least one happy-path and one violation-path test."),
            ("T", "010", "TR-16", "Hook plumbing. .git/hooks/{pre-commit, commit-msg, post-commit, pre-push} each invoke `python -m chathealthy_enforcement_manager <hook>`. .claude/settings.json wires SessionStart, UserPromptSubmit, PreToolUse, Stop, SessionEnd, InstructionsLoaded to the same manager with the matching hook_name."),
            ("T", "011", "TR-17", "Worker taxonomy supported by the framework: file scanning (pre-commit / pre-push), prompt-injection check (UserPromptSubmit — returns additionalContext), post-response audit (Stop), log publishing (UserPromptSubmit + Stop -> Kafka), boot orchestration (SessionStart), and mode selection (SessionStart). Each is a concrete subclass of EnforcementWorker, deployed as its own standalone executable."),
        ],
    },
    {
        "story_id": "EPIC-008-F-002-S-004",
        "name": "Scanning: file enumeration on hooks, SCOPES evaluation, scope precedence",
        "description": "Per V18 Sections 4.5 and 4.9. Workers receive resource candidates (staged or about-to-push files for the V1 ScanFilesEnforcementWorker), evaluate each via the base-class is_in_scope() walk against the SCOPES rows on the enforcement entry, and dispatch matched check methods. Scope precedence is fixed and lives entirely in the base class.",
        "size": "M",
        "status": "done",
        "approval": "approved",
        "orphan": False,
        "br_trs": [
            ("T", "001", "TR-5",  "Scope evaluation is fixed-precedence and lives entirely in the base class. For each (resource, function_name) the base class walks the matching rows in self.scopes (filtered by function_name) in this fixed order: excluded_exact, excluded_pattern, allowed_exact, allowed_pattern, then per-check SCOPE_DEFAULT. Subclasses do not override this walk. The default behavior (in scope vs. out of scope when no row matched) is declared per check via the per-method SCOPE_DEFAULT class attribute."),
            ("T", "002", "TR-9",  "ScanFilesEnforcementWorker (worked example, V1) lives at architecture/EngineeringRuleEnforcement/code/scan_files_enforcement_worker.py. It is a standalone Python script with a main() that takes enforcement_id as a CLI argument and exits with a status code. Its run() implements two domain checks synchronously, in this order, on every in-scope file: (a) _scan_http(file); (b) _validate_json(file). Both run inside the same subprocess. requires_lock = false. ScanFilesEnforcementWorker inherits the default _load_scopes() from EnforcementWorker."),
            ("T", "003", "TR-10", "ScanFilesEnforcementWorker scope rules are expressed as a flat three-column list in its enforcement entry under the scopes field. Rows are [function_name, scope_list_type, list_of_terms] where scope_list_type is one of the four canonical terms. The base class loads the list via the virtual _load_scopes() method (default returns self.entry.get(\"scopes\", [])). It resolves function_name to a bound method via getattr(self, row[0]) at startup, so a typo fails loudly. SCOPE_DEFAULT is per-check: False for _scan_http, True for _validate_json."),
            ("T", "004", "TR-13", "preCommitScan.py SHALL drop \".json\" from SCAN_EXTENSIONS and \\.json$ from HTTP_EXEMPT_PATTERNS once ScanFilesEnforcementWorker is the single source of truth. Once the manager dispatches all enforcement, preCommitScan.py is retired."),
        ],
    },
    {
        "story_id": "EPIC-008-F-002-S-005",
        "name": "HTTP vs HTTPS: _scan_http check and allowed_pattern URL handling",
        "description": "Per V18 BR-12, BR-13, TR-11. The _scan_http check on ScanFilesEnforcementWorker enforces that production source code, documentation, and deployment configuration MUST NOT contain http:// URLs unless the URL itself matches an allowed_pattern row on the enforcement entry's scopes (loopback, link-local metadata, w3.org, etc.). Files intended to carry http:// content as legitimate data are listed in allowed_exact / allowed_pattern rows. Other files are exempted via excluded_exact / excluded_pattern.",
        "size": "S",
        "status": "done",
        "approval": "approved",
        "orphan": False,
        "br_trs": [
            ("B", "001", "BR-12", "Files that are intended to carry plain text \"http:\" as legitimate content — for example, JSON files whose purpose is to record example URLs, configuration files that list http endpoints by design, fixtures, and similar data files — MUST be allowed to contain the term \"http:\". These files are listed on the enforcement entry's allowed by exact match rows, or matched by the enforcement entry's allowed by pattern rows, and they will not be flagged as a violation."),
            ("B", "002", "BR-13", "Files that are intended to be production source code, documentation, deployment configuration, or any other artifact that should not embed insecure http URLs MUST NOT contain the term \"http:\" unless the URL itself is on the enforcement entry's allowed by pattern rows (for example, loopback addresses, link-local metadata endpoints, or known public standards URLs). Files of this kind that are deliberately exempt for any reason MUST appear on the enforcement entry's excluded by exact match rows or be matched by the enforcement entry's excluded by pattern rows. Any other file found to contain the term \"http:\" is a violation and the commit MUST be blocked."),
            ("T", "001", "TR-11", "The set of files allowed to contain \"http:\" is exactly the union of the enforcement entry's allowed_exact rows and the files matched by the entry's allowed_pattern rows for _scan_http. The set of files exempt from the scan entirely is exactly the union of the entry's excluded_exact rows and the files matched by the entry's excluded_pattern rows for _scan_http. Any file not matched by any of those four scope_list_type rows that contains the term \"http:\" — except where the URL itself matches an allowed_pattern row for _scan_http — is a violation."),
        ],
    },
    {
        "story_id": "EPIC-008-F-002-S-006",
        "name": "JSON validation: _validate_json check, $schema web-fetch resolver, meta-schema carve-out, in-run cache",
        "description": "Per V18 BR-11, TR-12, Sections 4.9.3 and 4.9.3.1. The _validate_json check on ScanFilesEnforcementWorker validates every governed JSON file under brain/machine_artifacts/content/ against the schema declared in its $schema field, using jsonschema Draft 2020-12. Schema URLs are resolved by HTTP GET (5s timeout, in-run cache); the JSON Schema 2020-12 meta-schema is the single carve-out, served from a local copy at Website/schemas/standard/. Files with $schema missing that are not explicitly excluded MUST FAIL LOUD.",
        "size": "M",
        "status": "done",
        "approval": "approved",
        "orphan": False,
        "br_trs": [
            ("B", "001", "BR-11", "Every file the system treats as governed JSON content MUST validate cleanly against the schema declared in its $schema field. The set of governed JSON files is defined using only the four permitted scope-list terms (allowed_exact, allowed_pattern, excluded_exact, excluded_pattern). The system MUST NOT trust data from a JSON file that fails schema validation, and the framework MUST treat validation failure as a governance violation. JSON validation is the load-bearing gate that protects the entire governed-data layer from silent drift."),
            ("T", "001", "TR-12", "Governed JSON content lives under brain/machine_artifacts/content/. Each governed JSON file MUST declare its schema URL in a top-level $schema field. JSON parsing uses Python's standard library json module. Schema validation uses the jsonschema package against JSON Schema Draft 2020-12. Validation failure produces a violation record per TR-6 and contributes to the worker's exit code (EXIT_VIOLATIONS_FOUND = 1). Files identified by the worker's excluded_exact or excluded_pattern lists are skipped silently; files in scope without a $schema field MUST FAIL LOUD. There is no opt-out, and there is no quiet passing of unschemaed JSON content. The JSON Schema 2020-12 meta-schema URL is the single resolver carve-out, served from a local copy at Website/schemas/standard/json-schema-2020-12-meta.json. All other $schema URLs are resolved by HTTP GET with a 5-second timeout and per-run cache."),
        ],
    },
]


def make_req(story_id: str, btype: str, num: str, name: str, requirement: str) -> dict:
    return {
        "req_id": f"{story_id}-REQ-{btype}-{num}",
        "name": name,
        "requirement": requirement,
        "priority": "must-have",
        "status": "done",
        "approval": "approved",
        "orphan": False,
        "depends_on": [],
        "pytests": {
            "pytest": [
                {
                    "pytest_type": "ORPHAN",
                    "orphan": True,
                    "file": "ORPHAN",
                }
            ]
        },
    }


def build_story(s: dict) -> dict:
    out = {
        "story_id": s["story_id"],
        "name": s["name"],
        "description": s["description"],
        "size": s["size"],
        "status": s["status"],
        "approval": s["approval"],
        "orphan": s["orphan"],
        "requirements": {
            "requirement": [
                make_req(s["story_id"], btype, num, f"{label}: {trbr_id}", body)
                for (btype, num, trbr_id, body) in s["br_trs"]
                for label in [trbr_id.split("-")[0]]  # "BR" or "TR"
            ]
        },
    }
    return out


# ---- IDs to retire ----
RETIRED = {
    # Story-level retires
    "EPIC-008-F-008-S-002": ("EPIC-008-F-002-S-005", "Security Code Scan story superseded by HTTP vs HTTPS story under F-002"),
    "EPIC-008-F-008-S-004": ("EPIC-008-F-002-S-006", "JSON Scan story superseded by JSON validation story under F-002"),
    # Reqs under retired stories
    "EPIC-008-F-008-S-002-REQ-B-001": ("EPIC-008-F-002-S-005-REQ-B-002", "Pre-commit HTTP-block requirement superseded by BR-13 (now S-005-REQ-B-002)"),
    "EPIC-008-F-008-S-002-REQ-T-001": ("EPIC-008-F-002-S-005-REQ-T-001", "preCommitScan.py argument-handling story superseded by TR-11 web-fetch + scope walk"),
    "EPIC-008-F-008-S-004-REQ-B-001": ("EPIC-008-F-002-S-006-REQ-B-001", "Brain JSON pre-commit gate superseded by BR-11 (now S-006-REQ-B-001)"),
    "EPIC-008-F-008-S-004-REQ-B-002": ("EPIC-008-F-002-S-003-REQ-B-001", "'Pre-commit single gate' superseded by BR-1 (rules carry enforcement entries) and BR-3 (framework hook-agnostic)"),
    "EPIC-008-F-008-S-004-REQ-T-001": ("EPIC-008-F-002-S-006-REQ-T-001", "Plural-array validation requirement repointed to TR-12 (governed JSON validation under brain/machine_artifacts/content/)"),
    "EPIC-008-F-008-S-004-REQ-T-002": ("EPIC-008-F-002-S-006-REQ-T-001", "'Single name label' requirement repointed to TR-12 (schema validation gate); the 'name' label rule itself is enforced by the brain JSON's schema"),
    "EPIC-008-F-008-S-004-REQ-T-003": ("EPIC-008-F-002-S-003-REQ-T-001", "'Engineering rule MUST trace to approved requirement' repointed to TR-1 (enforcement entry schema fields including rule_id linkage)"),
}


def main():
    with open(BACKLOG, encoding="utf-8") as f:
        bl = json.load(f)

    # 1. Build the 4 new stories
    new_story_objs = [build_story(s) for s in NEW_STORIES]

    # 2. Inject under EPIC-008-F-002 features.feature.stories.story
    for epic in bl["epics"]["epic"]:
        if epic.get("epic_id") == "EPIC-008":
            for feat in epic.get("features", {}).get("feature", []):
                if feat.get("feature_id") == "EPIC-008-F-002":
                    existing = feat["stories"]["story"]
                    feat["stories"]["story"] = existing + new_story_objs

    # 3. Remove retired stories from EPIC-008-F-008
    retired_story_ids = {sid for sid in RETIRED if "-REQ-" not in sid}
    for epic in bl["epics"]["epic"]:
        if epic.get("epic_id") == "EPIC-008":
            for feat in epic.get("features", {}).get("feature", []):
                if feat.get("feature_id") == "EPIC-008-F-008":
                    feat["stories"]["story"] = [
                        s for s in feat["stories"]["story"]
                        if s.get("story_id") not in retired_story_ids
                    ]

    # 4. Repoint depends_on across the entire backlog
    for epic in bl["epics"]["epic"]:
        for feat in epic.get("features", {}).get("feature", []):
            for story in feat.get("stories", {}).get("story", []):
                for req in story.get("requirements", {}).get("requirement", []):
                    new_deps = []
                    for d in req.get("depends_on", []):
                        sid = d.get("story_id")
                        if sid in RETIRED:
                            new_sid, _ = RETIRED[sid]
                            new_deps.append({"story_id": new_sid, "reason": d.get("reason", "")})
                        else:
                            new_deps.append(d)
                    req["depends_on"] = new_deps

    # 5. Write back
    with open(BACKLOG, "w", encoding="utf-8") as f:
        json.dump(bl, f, indent=2, ensure_ascii=False)

    # 6. Write mapping table
    mapping = {
        "retired_to_new": {old: {"new_id": new, "reason": reason} for old, (new, reason) in RETIRED.items()},
        "new_stories": [s["story_id"] for s in NEW_STORIES],
        "br_tr_assignment": {
            s["story_id"]: [tr_id for (_, _, tr_id, _) in s["br_trs"]]
            for s in NEW_STORIES
        },
    }
    MAPPING_OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(MAPPING_OUT, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

    print("Edited", BACKLOG)
    print("Mapping ->", MAPPING_OUT)
    print(f"New stories created: {[s['story_id'] for s in NEW_STORIES]}")
    print(f"Retired ids: {sorted(RETIRED.keys())}")


if __name__ == "__main__":
    main()
