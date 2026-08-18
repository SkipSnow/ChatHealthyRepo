"""scan_data_migration_compliance_worker.py — Rule-065-ENF-009.

EPIC-010-F-108-S-001-REQ-B-010: an engineering rule parses the migration file
with an LLM and guarantees that every requirement of that story is enforced by
the code. A file that does not satisfy every requirement is not committed.

The requirements are read from agile_backlog.json at scan time rather than
copied here. The backlog is the source of truth, so a requirement added,
reworded or removed changes what this worker demands without anyone editing
it — and a worker carrying its own stale copy of a story is the failure mode
this avoids.

Two checks run, in this order:

  1. Structural, and free. The migration file is declared in the manifest
     exactly once, at the path it actually occupies. An unregistered
     migrator is a back door and needs no LLM to see.

  2. Semantic, via the LLM. The migration file and the full requirement text
     go to the model, which answers per requirement: is this requirement
     enforced by this code? Any answer that is not yes rejects the commit.

REQ-B-009 -- exactly one file in the repository performs migration -- is
NOT enforced here, and saying so is better than a check that pretends. A
substring scan for other writers convicted eight bystanders; an AST scan
tightened to remove them then missed a real second migrator that hides its
database behind an f-string. Whether an arbitrary file performs migration is
not decidable by reading it statically. What IS enforced is that the declared
migrator is declared once and satisfies every requirement put to the model.

There is no pass-on-LLM-failure path. A file whose compliance could not be
established is not a file known to comply, and REQ-B-010 permits committing
only what satisfies every requirement.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.gemini import GeminiModel
from pydantic_ai.providers.google_gla import GoogleGLAProvider

try:
    from .enforcement_worker import (
        EnforcementWorker, ViolationRecord, EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )
except ImportError:
    from enforcement_worker import (  # noqa: E402
        EnforcementWorker, ViolationRecord, EXIT_OK, EXIT_VIOLATIONS_FOUND,
    )

_RULE_ID = "Rule-065"
_STORY_ID = "EPIC-010-F-108-S-001"
_MIGRATION_FILE = "pipeline/Code/data_migration.py"
_BACKLOG = "brain/machine_artifacts/content/agile_backlog.json"
_MANIFEST = "brain/machine_artifacts/content/deployment_architecture.json"
_MODEL_NAME = "gemini-2.5-flash"

_SYSTEM_PROMPT = """You audit one Python file against one requirement.

You are given a requirement from a software backlog and the complete source of
the file that is supposed to enforce it. Decide whether the code as written
enforces the requirement.

Answer enforced=true only when the code makes the required condition hold. If
the requirement describes something the file cannot decide on its own -- a
property of other files, of a database role, or of the wider system -- and the
file does nothing that contradicts it, answer enforced=true and say in your
reason which part is outside the file.

Answer enforced=false when the code contradicts the requirement, or when it
claims to do something it does not do.

Your reason must be one sentence naming the specific code that decided it."""


def _ch_exception():
    """ChatHealthyException, resolved without assuming the library is on the
    path. Enforcement workers are spawned as bare scripts by the manager."""
    import sys as _sys
    for _p in Path(__file__).resolve().parents:
        if (_p / ".git").exists():
            _lib = _p / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


class Verdict(BaseModel):
    enforced: bool = Field(description="does the code enforce this requirement")
    reason: str = Field(description="one sentence naming the deciding code")


class ScanDataMigrationComplianceWorker(EnforcementWorker):
    """Rule-065: the migration file satisfies every requirement of its story."""

    SCOPE_DEFAULT: bool = False

    def __init__(self, enforcement_id: str) -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0
        self._agent: Agent | None = None

    def _repo_root(self) -> Path:
        here = Path(__file__).resolve()
        for parent in (here, *here.parents):
            if (parent / ".git").is_dir():
                return parent
        return here.parents[3]

    def _requirements(self) -> list[tuple[str, str, str]]:
        """(req_id, name, text) for every requirement of the story."""
        backlog = json.loads(
            (self._repo_root() / _BACKLOG).read_text(encoding="utf-8"))
        for epic in backlog["epics"]["epic"]:
            for feature in epic.get("features", {}).get("feature", []):
                for story in feature.get("stories", {}).get("story", []):
                    if story.get("story_id") != _STORY_ID:
                        continue
                    return [
                        (r["req_id"], r.get("name", ""), r["requirement"])
                        for r in story["requirements"]["requirement"]
                    ]
        return []

    def _auditor(self) -> Agent:
        if self._agent is None:
            self._agent = Agent(
                GeminiModel(_MODEL_NAME,
                            provider=GoogleGLAProvider(api_key=self._api_key())),
                output_type=Verdict,
                system_prompt=_SYSTEM_PROMPT,
            )
        return self._agent

    def _api_key(self) -> str:
        from dotenv import dotenv_values
        values = dotenv_values(self._repo_root() / ".env")
        key = values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY")
        if not key:
            raise _ch_exception()(
                mode="config_error",
                component="ScanDataMigrationComplianceWorker",
                message=("GEMINI_API_KEY / GOOGLE_API_KEY absent from .env; "
                         f"{_STORY_ID} compliance cannot be established"))
        return key

    def _declared_locations(self) -> list[tuple[str, str]]:
        """(target_id, package_id) for every manifest declaration whose
        source_location is the migration file."""
        manifest = json.loads(
            (self._repo_root() / _MANIFEST).read_text(encoding="utf-8"))
        found = []
        for target in manifest.get("DeploymentTargetRecord", []):
            target_id = target.get("target_id", "")
            for binding in target.get("environments", []):
                for package in binding.get("packages", []) or []:
                    package_id = package.get("package_id", "")
                    for declared in package.get("files", []) or []:
                        location = (declared.get("source_location") or "")
                        if location.replace("\\", "/") == _MIGRATION_FILE:
                            found.append((target_id, package_id))
        return found

    def _manifest_violation(self) -> str:
        """Empty when the migration file is declared once, at its real path.

        The manifest is what binds this code to a place the deploy knows
        about. A file that migrates data to the serving cluster and appears
        in no manifest is a back door: nothing ships it, nothing audits it,
        and nothing says it should exist.
        """
        declared = self._declared_locations()
        if not declared:
            return (f"{_MIGRATION_FILE} is declared in no target or package of "
                    f"{_MANIFEST}; code that writes to the serving cluster and "
                    f"is registered nowhere is an ungoverned path")
        if len(set(declared)) > 1:
            places = ", ".join(f"{t}/{p}" for t, p in sorted(set(declared)))
            return (f"{_MIGRATION_FILE} is declared in more than one place "
                    f"({places}); exactly one target and package owns it")
        return ""

    def _audit(self, source: str, req_id: str, name: str, text: str) -> Verdict:
        from chathealthy_lib.llm import run_llm_sync
        prompt = (
            f"REQUIREMENT {req_id} — {name}\n\n{text}\n\n"
            f"FILE {_MIGRATION_FILE}\n\n{source}"
        )
        result = run_llm_sync(
            self._auditor(), prompt,
            call_site="scan_data_migration_compliance_worker.audit",
            provider="google",
            server="pre-commit",
            component="ScanDataMigrationComplianceWorker",
        )
        return result.output

    def run(self) -> int:
        staged = {f.replace("\\", "/") for f in self.files}
        any_violations = False

        if _MIGRATION_FILE not in staged:
            return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

        path = self._repo_root() / _MIGRATION_FILE
        if not path.is_file():
            return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

        source = path.read_text(encoding="utf-8")
        self.files_scanned += 1

        manifest_problem = self._manifest_violation()
        if manifest_problem:
            self._emit_violation(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=_RULE_ID,
                resource=_MIGRATION_FILE,
                message=manifest_problem,
            ))
            self.violation_count += 1
            any_violations = True

        requirements = self._requirements()
        if not requirements:
            self._emit_violation(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=_RULE_ID,
                resource=_MIGRATION_FILE,
                message=(
                    f"{_STORY_ID} carries no requirements in {_BACKLOG}; "
                    f"compliance of {_MIGRATION_FILE} cannot be established"),
            ))
            return EXIT_VIOLATIONS_FOUND

        for req_id, name, text in requirements:
            try:
                verdict = self._audit(source, req_id, name, text)
            except Exception as exc:  # noqa: BLE001
                self._emit_violation(ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=_RULE_ID,
                    resource=_MIGRATION_FILE,
                    message=(
                        f"{req_id} could not be audited "
                        f"({type(exc).__name__}: {str(exc)[:200]}); a file whose "
                        f"compliance is unestablished is not committed"),
                ))
                self.violation_count += 1
                any_violations = True
                continue

            if not verdict.enforced:
                self._emit_violation(ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=_RULE_ID,
                    resource=_MIGRATION_FILE,
                    message=f"{req_id} ({name}) not enforced: {verdict.reason}",
                ))
                self.violation_count += 1
                any_violations = True

        return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK


if __name__ == "__main__":
    import sys
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-009"
    sys.exit(ScanDataMigrationComplianceWorker(enforcement_id).run())
