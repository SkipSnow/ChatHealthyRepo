"""ScanFilesEnforcementWorker — V1 concrete worker (worked example).

Binding contract: CH-EPIC8-Feachure-002-EngineeringRulesEnforcement-designV18.docx
    §4.9    (worked example: ScanFilesEnforcementWorker on pre-commit)
    §4.9.1  (class-member table)
    §4.9.2  (worked-example scope rows — live on the enforcement entry)
    §4.9.3  (JSON-validation implementation contract — web-fetch resolver)
    §4.9.3.1 (carve-out policy for contractually-frozen external schemas)
    TR-9, TR-10, TR-11, TR-12

Two checks, both synchronous, in this order per file:
    1. _scan_http(file)            — regex scan for http://; allowed_pattern URLs OK
    2. _validate_json(data, schema) — jsonschema.Draft202012Validator (V18 §4.9.3)

V18 schema-resolution model (replaces V17 URL-pattern resolver and the prior
draft's pre-build-at-init registry):
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
(V18 §4.9.1 explicitly forbids overriding _load_scopes() here).
"""

from __future__ import annotations

import json
import os
import re
import socket
import sys
import urllib.error
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


# Regex matching plain http:// URLs in file content (V18 §4.9 / TR-11).
# Captured group 0 is the full URL up to the first whitespace / quote / angle
# bracket / comma / closing paren. Used both for "is there an http: in this
# file" and "what was the actual URL so we can pattern-allow it".
_HTTP_URL_RE = re.compile(r"http://[^\s\"'<>,()]+")

# JSON Schema 2020-12 meta-schema URL — schema files declare this in their
# top-level $schema. Frozen per spec; served from a local copy via the
# carve-out map (V18 §4.9.3.1).
_META_SCHEMA_URL: str = "https://json-schema.org/draft/2020-12/schema"

# Local cached copy of the JSON Schema 2020-12 meta-schema (V18 §4.9.3.1).
# This is the ONLY entry currently in the carve-out map. Adding others requires
# documented contractual freeze of the third-party schema URL.
_META_SCHEMA_LOCAL_PATH: Path = (
    PROJECT_ROOT
    / "Website"
    / "schemas"
    / "standard"
    / "json-schema-2020-12-meta.json"
)

# HTTP fetch timeout (seconds) for runtime $schema resolution (V18 §4.9.3).
_SCHEMA_FETCH_TIMEOUT_SECONDS: int = 5

# HTTP fetch headers for runtime $schema resolution (V18 §4.9.3).
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

    # SCOPE_DEFAULT is per-check (V18 Table 9 row 1 / §4.5):
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
        "_scan_http": False,
        "_validate_json": False,
    }
    # Class-level fallback (used if a check method is added without a per-
    # method default declared in SCOPE_DEFAULTS).
    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        # Counters surfaced by the base-class telemetry envelope (TR-7).
        self.files_scanned: int = 0
        self.violation_count: int = 0
        # Carve-out for contractually-frozen third-party schemas (V18 §4.9.3.1).
        # Only the JSON Schema 2020-12 meta-schema lives here today; its URL is
        # immutable per the spec maintainers' contract (any change requires a
        # new draft URL). ChatHealthy schemas are NEVER carve-outs — they
        # always web-fetch.
        self._frozen_external_schemas: dict[str, dict[str, Any]] = (
            self._load_frozen_external_schemas()
        )
        # Per-run cache for runtime web-fetch deduplication (V18 §4.9.3 step 3).
        # Cleared at the start of each run().
        self._fetched_schema_cache: dict[str, dict[str, Any]] = {}

    # ────────────────────────────────────────────────────────────────────────
    # Carve-out loader (V18 §4.9.3.1)
    # ────────────────────────────────────────────────────────────────────────
    def _load_frozen_external_schemas(self) -> dict[str, dict[str, Any]]:
        """Build the {url: schema} map for contractually-frozen external schemas.

        V18 §4.9.3.1 carve-out policy: any URL in this map MUST be a
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
        """For each staged file, _scan_http then _validate_json (V18 Table 9 row 2).

        Synchronous. Multi-threading inside run() is a Phase-6 enhancement
        and is NOT V1 (V18 §4.9 / Phase-6 backlog).
        """
        # Per-run cache lifetime: the cache is dedup-only. Clearing at the
        # start of run() guarantees no leakage across runs in long-lived test
        # processes.
        self._fetched_schema_cache = {}

        files = self._staged_files()
        any_violations = False
        for file_path in files:
            self.files_scanned += 1

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

        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

    # ────────────────────────────────────────────────────────────────────────
    # Resource enumeration
    # ────────────────────────────────────────────────────────────────────────
    def _staged_files(self) -> list[str]:
        """Return repo-relative paths of files for this hook to scan.

        On pre-commit: the staged files (`git diff --cached --name-only ...`).
        On any other hook the scan target list is the empty list — workers
        for those hooks will populate it via their own enumeration. For V1
        the worker is wired only to pre-commit per V18 §4.9.

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
    # _scan_http  (V18 Table 9 row 3 / TR-11)
    # ────────────────────────────────────────────────────────────────────────
    def _scan_http(self, file_path: str) -> list[ViolationRecord]:
        """Regex-scan for http://; emit a violation for each URL not allowed.

        Per V18 / TR-11 the base class has already cleared file-level scope
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
    # JSON validation orchestration — file parse + schema resolution
    # ────────────────────────────────────────────────────────────────────────
    def _check_one_file_json(self, file_path: str) -> list[ViolationRecord]:
        """Run JSON validation against one in-scope file (V18 §4.9.3).

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
    # Schema resolution — carve-out, per-run cache, web fetch (V18 §4.9.3)
    # ────────────────────────────────────────────────────────────────────────
    def _resolve_schema(
        self,
        file_path: str,
        schema_url: str,
    ) -> tuple[dict[str, Any] | None, ViolationRecord | None]:
        """Resolve $schema URL → schema dict (V18 §4.9.3 step 3).

        Single responsibility: take a URL, return either (schema, None) or
        (None, ViolationRecord). Does not validate; does not parse data.
        """
        # 1. Carve-out for contractually-frozen externals (V18 §4.9.3.1).
        if schema_url in self._frozen_external_schemas:
            return self._frozen_external_schemas[schema_url], None

        # 2. Per-run cache (dedupe within a single run()).
        if schema_url in self._fetched_schema_cache:
            return self._fetched_schema_cache[schema_url], None

        # 3. Web fetch.
        return self._fetch_schema_from_url(file_path, schema_url)

    def _fetch_schema_from_url(
        self,
        file_path: str,
        schema_url: str,
    ) -> tuple[dict[str, Any] | None, ViolationRecord | None]:
        """HTTP GET schema_url with a 5s timeout; cache on success.

        Per V18 §4.9.3 step 3: fetch failure (URLError, HTTPError, timeout,
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
        """Build a ViolationRecord for a $schema fetch failure (V18 §4.9.3)."""
        return ViolationRecord(
            enforcement_id=self.enforcement_id,
            rule_id=self.rule_id,
            resource=file_path,
            message=f"schema fetch failed: {schema_url}: {reason}",
            severity="error",
        )

    # ────────────────────────────────────────────────────────────────────────
    # _validate_json — the single thing this method does (V18 §4.9.3 / TR-12)
    # ────────────────────────────────────────────────────────────────────────
    def _validate_json(
        self,
        data: Any,
        schema: dict[str, Any],
    ) -> list[jsonschema.exceptions.ValidationError]:
        """JSON validation per V18 §4.9.3 / TR-12.

        jsonschema.Draft202012Validator is the SOLE arbiter of validity. Do not invent.
        See V18 §4.9.3 for the binding contract.
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
