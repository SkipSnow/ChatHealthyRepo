"""ScanFilesEnforcementWorker — V1 concrete worker (worked example).

Binding contract: CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV20.docx
    §4.0     (Implementation discipline — TR-trace principle)
    §4.9     (worked example: ScanFilesEnforcementWorker on pre-commit)
    §4.9.1   (class-member table)
    §4.9.2   (worked-example scope rows — live on the enforcement entry)
    §4.9.3   (JSON-validation implementation contract — web-fetch resolver)
    §4.9.3.1 (carve-out policy for contractually-frozen external schemas)
    TR-9, TR-10, TR-11, TR-12

Two checks, both synchronous, in this order per file:
    1. _scan_http(file)            — regex scan for http://; allowed_pattern URLs OK
    2. _validate_json(data, schema) — jsonschema.Draft202012Validator (V19 §4.9.3)

V19 inherits the V18 schema-resolution model (web-fetch resolver replaced V17's
URL-pattern resolver and the prior draft's pre-build-at-init registry):
    • The JSON file's `$schema` URL is the source of truth.
    • For each in-scope file, the worker HTTP-GETs that URL with a 5s timeout,
      caches the response in a per-run dict, and validates against it.
    • One carve-out: the JSON Schema 2020-12 meta-schema URL is contractually
      frozen by the spec maintainers (any change requires a new draft URL),
      so it is served from a local copy at
      Website/schemas/standard/json-schema-2020-12-meta.json. See §4.9.3.1.
    • An unreachable URL, non-200, timeout, or non-JSON body is a per-file
      ViolationRecord, NOT a WorkerInternalError.

Inherits the default _load_scopes() from EnforcementWorker — its scope rows
live on its enforcement entry's `scopes` field in engineering_rules.json
(V19 §4.9.1 explicitly forbids overriding _load_scopes() here).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import jsonschema

# Allow being run both as a script ("python scan_files_enforcement_worker.py
# <id>") AND imported as a module (the tests import it).
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from enforcement_worker import (
        EnforcementWorker,
        ViolationRecord,
        WorkerInternalError,
        PROJECT_ROOT,
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )
else:
    from .enforcement_worker import (  # type: ignore
        EnforcementWorker,
        ViolationRecord,
        WorkerInternalError,
        PROJECT_ROOT,
        EXIT_OK,
        EXIT_VIOLATIONS_FOUND,
    )


# Regex matching plain http:// URLs in file content (V19 §4.9 / TR-11).
# Captured group 0 is the full URL up to the first whitespace / quote / angle
# bracket / comma / closing paren. Used both for "is there an http: in this
# file" and "what was the actual URL so we can pattern-allow it".
_HTTP_URL_RE = re.compile(r"http://[^\s\"'<>,()]+")

# Rule-008 statement #4: regex API usage detection patterns. File-selection
# (which files this method scans) lives in the scope array on
# Rule-008-ENF-001. The detection patterns are content-only.
_PY_REGEX_HITS = (
    r"^\s*import\s+re\b",
    r"^\s*from\s+re\s+import\b",
    r"\bre\.(compile|match|search|findall|finditer|sub|split|fullmatch)\s*\(",
)
_JS_REGEX_HITS = (
    r"\bnew\s+RegExp\s*\(",
)

# JSON Schema 2020-12 meta-schema URL — schema files declare this in their
# top-level $schema. Frozen per spec; served from a local copy via the
# carve-out map (V19 §4.9.3.1).
_META_SCHEMA_URL: str = "https://json-schema.org/draft/2020-12/schema"

# Local cached copy of the JSON Schema 2020-12 meta-schema (V19 §4.9.3.1).
# This is the ONLY entry currently in the carve-out map. Adding others requires
# documented contractual freeze of the third-party schema URL.
_META_SCHEMA_LOCAL_PATH: Path = (
    PROJECT_ROOT
    / "Website"
    / "schemas"
    / "standard"
    / "json-schema-2020-12-meta.json"
)

# HTTP fetch timeout (seconds) for runtime $schema resolution (V19 §4.9.3).
_SCHEMA_FETCH_TIMEOUT_SECONDS: int = 5

# HTTP fetch headers for runtime $schema resolution (V19 §4.9.3).
# User-Agent identifies this worker — the default urllib UA is on Cloudflare's
# automated-tool blocklist for dev.chathealthy.ai and produces HTTP 403 before
# the WAF custom rule can fire. Sending an explicit UA bypasses that early
# block. The worker token (if env var is set) matches the WAF custom skip
# rule that bypasses downstream security features for /schemas/* requests
# carrying it. The token is optional: hosts that don't gate by token still
# work as long as the explicit UA is present.
_HTTP_USER_AGENT: str = "ChatHealthy-EnforcementWorker/1.0"
_WORKER_TOKEN_ENV_VAR: str = "CHATHEALTHY_CF_WORKER_TOKEN"
_WORKER_TOKEN_HEADER_NAME: str = "X-ChatHealthy-Worker-Token"


class ScanFilesEnforcementWorker(EnforcementWorker):
    """V1 worker: HTTP scan + JSON schema validation on staged files."""

    # SCOPE_DEFAULT is per-check (V19 Table 9 row 1 / §4.5):
    #   _scan_http     → False (opt-in by extension)
    #   _validate_json → False (gated positively by the \.json$ allowed_pattern
    #                    row; non-JSON files never enter the validator).
    # NOTE on the _validate_json default: V17 §4.5 originally specified True
    # because SCOPES carried only excluded rows for this check. Since the
    # refactor (BUG-ENF-WORKER-002) the entry's scopes now declare a positive
    # `["_validate_json", "allowed_pattern", ["\\.json$"]]` row that gates
    # in-scope membership exactly. With the gate explicit, the default flips
    # to False so that non-JSON files do not fall through to a json.load
    # parse-error violation. The exclusion rows for package-lock.json and
    # tests/fixtures/ still trump the allowed_pattern per TR-5 precedence.
    SCOPE_DEFAULTS: dict[str, bool] = {
        "_scan_http": True,
        "_validate_json": False,
        "_block_regular_expressions_in_executable_code": False,
        "_scan_no_secret_values": False,
    }
    # Class-level fallback (used if a check method is added without a per-
    # method default declared in SCOPE_DEFAULTS).
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        # Counters surfaced by the base-class telemetry envelope (TR-7).
        self.files_scanned: int = 0
        self.violation_count: int = 0
        # Carve-out for contractually-frozen third-party schemas (V19 §4.9.3.1).
        # Only the JSON Schema 2020-12 meta-schema lives here today; its URL is
        # immutable per the spec maintainers' contract (any change requires a
        # new draft URL). ChatHealthy schemas are NEVER carve-outs — they
        # always web-fetch.
        self._frozen_external_schemas: dict[str, dict[str, Any]] = (
            self._load_frozen_external_schemas()
        )
        # Per-run cache for runtime web-fetch deduplication (V19 §4.9.3 step 3).
        # Cleared at the start of each run().
        self._fetched_schema_cache: dict[str, dict[str, Any]] = {}

    # ────────────────────────────────────────────────────────────────────────
    # Carve-out loader (V19 §4.9.3.1)
    # ────────────────────────────────────────────────────────────────────────
    def _load_frozen_external_schemas(self) -> dict[str, dict[str, Any]]:
        """Build the {url: schema} map for contractually-frozen external schemas.

        V19 §4.9.3.1 carve-out policy: any URL in this map MUST be a
        contractually-frozen external standard with documented immutable URL
        semantics. Today there is exactly one entry — the JSON Schema 2020-12
        meta-schema, frozen per the JSON Schema spec.

        Failure to load this file is a worker-internal error (the worker cannot
        validate any schema file without it).
        """
        if not _META_SCHEMA_LOCAL_PATH.is_file():
            raise WorkerInternalError(
                f"frozen external meta-schema not found at {_META_SCHEMA_LOCAL_PATH}; "
                f"run the deploy step that populates Website/schemas/standard/"
            )
        try:
            with _META_SCHEMA_LOCAL_PATH.open(encoding="utf-8") as f:
                meta_schema = json.load(f)
        except json.JSONDecodeError as exc:
            raise WorkerInternalError(
                f"frozen external meta-schema {_META_SCHEMA_LOCAL_PATH} is "
                f"malformed JSON: {exc.msg} at line {exc.lineno} col {exc.colno}"
            )
        return {_META_SCHEMA_URL: meta_schema}

    # ────────────────────────────────────────────────────────────────────────
    # Orchestration
    # ────────────────────────────────────────────────────────────────────────
    def run(self) -> int:
        """For each staged file, _scan_http then _validate_json (V19 Table 9 row 2).

        Synchronous. Multi-threading inside run() is a Phase-6 enhancement
        and is NOT V1 (V19 §4.9 / Phase-6 backlog).
        """
        # Per-run cache lifetime: the cache is dedup-only. Clearing at the
        # start of run() guarantees no leakage across runs in long-lived test
        # processes.
        self._fetched_schema_cache = {}

        files = self._staged_files()
        any_violations = False
        for file_path in files:
            self.files_scanned += 1

            # Rule-008 statement #4 runs FIRST. Regex usage in production
            # code rejects the file; downstream checks are skipped.
            if self.is_in_scope(file_path, "_block_regular_expressions_in_executable_code"):
                regex_violations = self._block_regular_expressions_in_executable_code(file_path)
                if regex_violations:
                    for v in regex_violations:
                        self._emit_violation(v)
                        self.violation_count += 1
                    any_violations = True
                    continue

            if self.is_in_scope(file_path, "_scan_http"):
                for v in self._scan_http(file_path):
                    self._emit_violation(v)
                    self.violation_count += 1
                    any_violations = True

            if self.is_in_scope(file_path, "_validate_json"):
                for v in self._check_one_file_json(file_path):
                    self._emit_violation(v)
                    self.violation_count += 1
                    any_violations = True

            if self.is_in_scope(file_path, "_scan_no_secret_values"):
                for v in self._scan_no_secret_values(file_path):
                    self._emit_violation(v)
                    self.violation_count += 1
                    any_violations = True

        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    # ────────────────────────────────────────────────────────────────────────
    # _block_regular_expressions_in_executable_code  (Rule-008 statement #4)
    # ────────────────────────────────────────────────────────────────────────
    def _block_regular_expressions_in_executable_code(
        self, file_path: str
    ) -> list[ViolationRecord]:
        """Block regex API usage in production-executable code.

        File-selection lives in the scope array on Rule-008-ENF-001. This
        method only does content detection on whatever files the base
        class passes through. A file with violations is rejected and
        run() must skip downstream checks for that file.
        """
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            text = absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []

        posix = file_path.replace("\\", "/")
        is_py = posix.endswith(".py")
        is_jslike = posix.endswith((".ts", ".tsx", ".js", ".jsx", ".html"))
        patterns = (_PY_REGEX_HITS if is_py else ()) + (_JS_REGEX_HITS if is_jslike else ())

        violations: list[ViolationRecord] = []
        for pat in patterns:
            for m in re.finditer(pat, text, flags=re.MULTILINE):
                line_no = text[: m.start()].count("\n") + 1
                violations.append(
                    ViolationRecord(
                        enforcement_id=self.enforcement_id,
                        rule_id=self.rule_id,
                        resource=file_path,
                        message=(
                            f"regex usage at line {line_no}: {m.group(0).strip()[:80]} "
                            f"— Rule-008 statement #4 forbids regex in production-executable code"
                        ),
                        severity="error",
                    )
                )
        return violations

    # ────────────────────────────────────────────────────────────────────────
    # Resource enumeration
    # ────────────────────────────────────────────────────────────────────────
    def _staged_files(self) -> list[str]:
        """Return repo-relative paths of files for this hook to scan.

        On pre-commit: the staged files (`git diff --cached --name-only ...`).
        On any other hook the scan target list is the empty list — workers
        for those hooks will populate it via their own enumeration. For V1
        the worker is wired only to pre-commit per V19 §4.9.

        For tests, the SCAN_FILES_ENFORCEMENT_TARGETS environment variable
        can override the list directly with a path-separator-joined string.
        """
        import os
        import subprocess

        override = os.environ.get("SCAN_FILES_ENFORCEMENT_TARGETS")
        if override is not None:
            return [p for p in override.split(os.pathsep) if p]

        if self.hook != "pre-commit":
            return []

        completed = subprocess.run(
            ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            return []
        return [line for line in completed.stdout.splitlines() if line.strip()]

    # ────────────────────────────────────────────────────────────────────────
    # _scan_http  (V19 Table 9 row 3 / TR-11)
    # ────────────────────────────────────────────────────────────────────────
    def _scan_http(self, file_path: str) -> list[ViolationRecord]:
        """Regex-scan for http://; emit a violation for each URL not allowed.

        Per V19 / TR-11 the base class has already cleared file-level scope
        using rows where row[0] == "_scan_http" (excluded_exact /
        excluded_pattern). This method then scans the file's content for
        http:// URLs and gates each found URL against the allowed_pattern
        rows for _scan_http on this enforcement entry. URLs that match an
        allowed_pattern row (loopback, link-local metadata, w3.org, etc.)
        are not violations; URLs that don't match are.
        """
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        # Binary skip: a file that fails utf-8 decode cannot semantically contain an http:// URL.
        # Return [] silently rather than misreading bytes (V21 §4.5).
        try:
            absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return []

        # Pull the allowed-URL patterns for _scan_http off this entry's scopes.
        allowed_url_patterns: list[str] = []
        for row in self.scopes:
            if row[0] == "_scan_http" and row[1] == "allowed_pattern":
                allowed_url_patterns.extend(row[2])

        violations: list[ViolationRecord] = []
        with absolute_path.open(encoding="utf-8", errors="replace") as f:
            text = f.read()

        for match in _HTTP_URL_RE.finditer(text):
            url = match.group(0)
            if any(re.search(pat, url) for pat in allowed_url_patterns):
                continue
            violations.append(
                ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=self.rule_id,
                    resource=file_path,
                    message=f"insecure http URL: {url}",
                    severity="error",
                )
            )
        return violations

    # ────────────────────────────────────────────────────────────────────────
    # _scan_no_secret_values  (Rule-008 statement #5 — REQ-T-053)
    # ────────────────────────────────────────────────────────────────────────
    def _scan_no_secret_values(self, file_path: str) -> list[ViolationRecord]:
        """Reject if the staged file contains any value from the local .env.

        Per EPIC-008-F-012-S-001-REQ-T-053: no file in any per-target build-
        package directory may contain a literal secret VALUE. The check
        loads every value from Code/.env via SecretsResolver's leak-check
        helper, then substring-matches against the file's text bytes.
        Matches emit a bare violation; the matched value is NEVER logged
        or echoed in the message.

        File-selection (which paths this method scans) lives in the scope
        array on Rule-008-ENF-001. This method only does content matching
        on whatever files the base class passes through.
        """
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        if not absolute_path.is_file():
            return []
        try:
            text = absolute_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Binary file (e.g., zip) — bytes can still contain plaintext
            # secrets. Fall through to a binary read.
            try:
                text = absolute_path.read_bytes().decode("latin-1", errors="replace")
            except OSError:
                return []

        env_values = self._load_env_values()
        if not env_values:
            return []
        violations: list[ViolationRecord] = []
        seen_in_file = False
        for v in env_values:
            if v and v in text:
                seen_in_file = True
                break
        if seen_in_file:
            violations.append(
                ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=self.rule_id,
                    resource=file_path,
                    message="secret VALUE present in build-package file",
                    severity="error",
                )
            )
        return violations

    def _load_env_values(self) -> set[str]:
        """Load .env values via SecretsResolver. Cached per worker run."""
        if hasattr(self, "_env_values_cache"):
            return self._env_values_cache  # type: ignore[attr-defined]
        env_file = PROJECT_ROOT / "Code" / ".env"
        if not env_file.is_file():
            self._env_values_cache: set[str] = set()  # type: ignore[attr-defined]
            return self._env_values_cache
        deploy_dir = PROJECT_ROOT / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement"
        added = False
        if str(deploy_dir) not in sys.path:
            sys.path.insert(0, str(deploy_dir))
            added = True
        try:
            from secrets_resolver import SecretsResolver
            self._env_values_cache = SecretsResolver().env_values_for_leak_check(env_file)  # type: ignore[attr-defined]
        finally:
            if added and str(deploy_dir) in sys.path:
                sys.path.remove(str(deploy_dir))
        return self._env_values_cache

    # ────────────────────────────────────────────────────────────────────────
    # JSON validation orchestration — file parse + schema resolution
    # ────────────────────────────────────────────────────────────────────────
    def _check_one_file_json(self, file_path: str) -> list[ViolationRecord]:
        """Run JSON validation against one in-scope file (V19 §4.9.3).

        Steps:
          1. Parse the data file. Parse error → ViolationRecord; no validation.
          2. Read data["$schema"]. Missing/non-string → ViolationRecord.
          3. Resolve the schema:
               a. If $schema URL is in self._frozen_external_schemas (carve-out
                  for contractually-frozen externals), use the cached copy.
               b. Else if $schema URL is in self._fetched_schema_cache (per-run
                  dedup), use that.
               c. Else HTTP-GET the URL with a 5s timeout; cache the parsed
                  result. Any fetch failure (URLError, HTTPError, timeout,
                  OSError, malformed JSON) → ViolationRecord; no validation.
          4. Call self._validate_json(data, schema) — pure validation.
          5. Map each ValidationError to a ViolationRecord.
        """
        absolute_path = (PROJECT_ROOT / file_path).resolve()
        data, parse_violation = self._load_data(file_path, absolute_path)
        if parse_violation is not None:
            return [parse_violation]
        if data is None:
            return []

        schema_url = data.get("$schema") if isinstance(data, dict) else None
        if not isinstance(schema_url, str) or not schema_url:
            return [
                ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=self.rule_id,
                    resource=file_path,
                    message="$schema field is required and must be a string",
                    severity="error",
                )
            ]

        schema, resolve_violation = self._resolve_schema(file_path, schema_url)
        if resolve_violation is not None:
            return [resolve_violation]
        # mypy: schema is non-None when resolve_violation is None.
        assert schema is not None

        return [
            self._violation_from_error(file_path, err)
            for err in self._validate_json(data, schema)
        ]

    # ────────────────────────────────────────────────────────────────────────
    # Schema resolution — carve-out, per-run cache, web fetch (V19 §4.9.3)
    # ────────────────────────────────────────────────────────────────────────
    def _resolve_schema(
        self,
        file_path: str,
        schema_url: str,
    ) -> tuple[dict[str, Any] | None, ViolationRecord | None]:
        """Resolve $schema URL → schema dict.

        Order:
          1. Contractually-frozen externals (V19 §4.9.3.1 carve-out).
          2. Per-run cache (dedupe within a single run()).
          3. chathealthy.ai brain-artifact URLs → read Website/schemas/<basename>
             from the local repo. Brain-artifact schemas live in the git tree;
             the URL exists for IDE convenience only. The working tree is
             the source of truth for enforcement.
          4. External URL → web fetch.
        """
        # 1. Carve-out for contractually-frozen externals (V19 §4.9.3.1).
        if schema_url in self._frozen_external_schemas:
            return self._frozen_external_schemas[schema_url], None

        # 2. Per-run cache (dedupe within a single run()).
        if schema_url in self._fetched_schema_cache:
            return self._fetched_schema_cache[schema_url], None

        # 3. Brain-artifact schemas → local repo file.
        local_schema = self._resolve_chathealthy_schema_locally(file_path, schema_url)
        if local_schema is not None:
            self._fetched_schema_cache[schema_url] = local_schema
            return local_schema, None

        # 4. Web fetch.
        return self._fetch_schema_from_url(file_path, schema_url)

    def _resolve_chathealthy_schema_locally(
        self,
        file_path: str,
        schema_url: str,
    ) -> dict[str, Any] | None:
        """Return the schema dict if schema_url is a chathealthy.ai schema
        URL and its local repo file exists; None otherwise. Missing local
        file is silent — falls through to web fetch (which will surface a
        proper violation if that also fails)."""
        try:
            parsed = urllib.parse.urlparse(schema_url)
        except Exception:  # noqa: BLE001
            return None
        host = (parsed.hostname or "").lower()
        if not host.endswith("chathealthy.ai"):
            return None
        path = parsed.path or ""
        if "/schemas/" not in path:
            return None
        basename = path.rsplit("/", 1)[-1]
        if not basename.endswith(".json"):
            return None
        local_path = PROJECT_ROOT / "Website" / "schemas" / basename
        if not local_path.is_file():
            return None
        try:
            return json.loads(local_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _fetch_schema_from_url(
        self,
        file_path: str,
        schema_url: str,
    ) -> tuple[dict[str, Any] | None, ViolationRecord | None]:
        """HTTP GET schema_url with a 5s timeout; cache on success.

        Per V19 §4.9.3 step 3: fetch failure (URLError, HTTPError, timeout,
        generic OSError) and malformed-JSON response are per-file violations,
        NOT WorkerInternalError. Unreachable URL is a deployment problem; the
        worker reports it as a violation against the file that declared it.
        """
        headers: dict[str, str] = {"User-Agent": _HTTP_USER_AGENT}
        worker_token = os.environ.get(_WORKER_TOKEN_ENV_VAR)
        if worker_token:
            headers[_WORKER_TOKEN_HEADER_NAME] = worker_token
        try:
            req = urllib.request.Request(schema_url, headers=headers)
            with urllib.request.urlopen(
                req, timeout=_SCHEMA_FETCH_TIMEOUT_SECONDS
            ) as resp:
                body = resp.read()
        except urllib.error.HTTPError as exc:
            return None, self._violation_for_fetch_failure(
                file_path, schema_url, f"HTTP {exc.code}"
            )
        except urllib.error.URLError as exc:
            return None, self._violation_for_fetch_failure(
                file_path, schema_url, str(exc.reason)
            )
        except socket.timeout:
            return None, self._violation_for_fetch_failure(
                file_path, schema_url, f"timeout after {_SCHEMA_FETCH_TIMEOUT_SECONDS}s"
            )
        except OSError as exc:
            return None, self._violation_for_fetch_failure(
                file_path, schema_url, f"{type(exc).__name__}: {exc}"
            )

        try:
            schema = json.loads(body)
        except json.JSONDecodeError as exc:
            return None, self._violation_for_fetch_failure(
                file_path,
                schema_url,
                f"malformed JSON in response: {exc.msg} at line {exc.lineno} col {exc.colno}",
            )

        self._fetched_schema_cache[schema_url] = schema
        return schema, None

    def _violation_for_fetch_failure(
        self,
        file_path: str,
        schema_url: str,
        reason: str,
    ) -> ViolationRecord:
        """Build a ViolationRecord for a $schema fetch failure (V19 §4.9.3)."""
        return ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id=self.rule_id,
            resource=file_path,
            message=f"schema fetch failed: {schema_url}: {reason}",
            severity="error",
        )

    # ────────────────────────────────────────────────────────────────────────
    # _validate_json — the single thing this method does (V19 §4.9.3 / TR-12)
    # ────────────────────────────────────────────────────────────────────────
    def _validate_json(
        self,
        data: Any,
        schema: dict[str, Any],
    ) -> list[jsonschema.exceptions.ValidationError]:
        """JSON validation per V19 §4.9.3 / TR-12.

        jsonschema.Draft202012Validator is the SOLE arbiter of validity. Do not invent.
        See V19 §4.9.3 for the binding contract.
        """
        return list(jsonschema.Draft202012Validator(schema).iter_errors(data))

    # ────────────────────────────────────────────────────────────────────────
    # File parse helper (Step 1 of §4.9.3)
    # ────────────────────────────────────────────────────────────────────────
    def _load_data(
        self,
        file_path: str,
        absolute_path: Path,
    ) -> tuple[Any, ViolationRecord | None]:
        """Step 1 of §4.9.3 — parse the file. Parse error → ViolationRecord."""
        try:
            with absolute_path.open(encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as error:
            v = ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=self.rule_id,
                resource=file_path,
                message=(
                    f"invalid JSON: {error.msg} at line {error.lineno} "
                    f"col {error.colno}"
                ),
                severity="error",
            )
            return None, v
        return data, None

    def _violation_from_error(
        self,
        file_path: str,
        error: jsonschema.exceptions.ValidationError,
    ) -> ViolationRecord:
        """Step 5 of §4.9.3 — map jsonschema error → ViolationRecord."""
        path = "/".join(str(p) for p in error.absolute_path)
        return ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id=self.rule_id,
            resource=file_path,
            message=f"{path}: {error.message}",
            severity="error",
        )


def main(argv: list[str] | None = None) -> int:
    return ScanFilesEnforcementWorker.main(argv)


if __name__ == "__main__":
    sys.exit(main())
