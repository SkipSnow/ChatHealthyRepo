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

import ast
import json
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

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
_MODEL_NAME = "gpt-5.5"
# Rounds, not retries-on-error: each round re-asks only what came back
# undetermined and keeps every verdict already settled. Ten attempts, and a
# requirement still undetermined after ten is a requirement not established,
# so the commit does not happen.
_AUDIT_ROUNDS = 10
# A verdict under this is not an answer, it is an impression. Discarded and
# asked again; still short after every round, the commit does not happen.
_MIN_CONFIDENCE = 90
# The override. Naming a requirement here waives it for one commit, and the
# waiver is recorded as loudly as a violation -- there is no quiet way to skip
# one. Both variables are required: the ids, and a reason in the operator's
# own words.
_OVERRIDE_IDS_ENV = "CH_ENF009_OVERRIDE_REQUIREMENTS"
_OVERRIDE_REASON_ENV = "CH_ENF009_OVERRIDE_REASON"

_SYSTEM_PROMPT = """You are the last gate before code enters a healthcare
company's repository. You decide, requirement by requirement, whether one file
does what its specification says. If you pass code that does not comply, it
ships; if you fail compliant code, you block an engineer for nothing. Both are
real costs, so decide from evidence rather than impression.

THE SYSTEM YOU ARE AUDITING

ChatHealthy runs two MongoDB Atlas clusters. ChatHealthyDataPipelines holds
data being built. ChatHealthyFrontEnd holds the data that serves real users
looking for healthcare providers. A migration copies one collection from the
pipeline cluster's PipelinePublicHealthData database to the front-end
cluster's PublicHealthData database, under the same name.

The identity that performs it, dataMigrator, holds find, insert,
createCollection and createIndex on the destination and nothing else -- no
update, no delete, no drop. So the migration can add and can never alter or
remove. This matters when you judge requirements about not damaging existing
data: some are guaranteed by that database role rather than by the code, and
code that simply never calls a destructive operation is consistent with the
requirement rather than in violation of it.

YOUR TOOLS -- USE THEM

You can read any file in the repository and search the whole repository for
text. Several requirements are claims about the repository as a whole, not
about the file in front of you: "no other process writes to the front-end
database", "exactly one file performs migration". You cannot answer those by
reading the audited file. Search for the database name, for insert calls, for
getConnection to the front-end cluster, and see what you find. An unsearched
claim answered enforced=true is a guess, and a guess here is worse than
useless because it wears the authority of a gate.

READ THE STORY FIRST

You are given the epic, feature and story the requirements belong to, in the
words of the people who wrote them. A requirement is a sentence from that
story, not a standalone rule, and the story tells you what it is protecting.
Judge each requirement for what it is trying to prevent, not for the most
literal reading of its words -- but never against what it plainly says.

HOW TO DECIDE EACH ONE

enforced=true when the code makes the required condition hold, or when the
requirement is guaranteed outside the file -- by the database role, by the
deployment, by another component -- and nothing in this file contradicts it.
Say which, in the reason.

enforced=false when the code contradicts the requirement, when it claims in a
comment or docstring to do something the code does not do, or when you looked
and found a violation elsewhere in the repository.

Prefer false over true when you are genuinely unsure AND the requirement is
about data safety or authorization. Prefer true over false when a requirement
plainly describes something outside this file's control and the file is silent
about it. The source you are given has had every comment and docstring removed. You
are reading what executes and nothing that claims to. There are no hints in
it and none are coming.

CONFIDENCE

Every verdict carries a confidence from 0 to 100. Anything below 90 is
discarded and that requirement is put to you again, so a low number costs
nothing and buys another look. A high number on a verdict you did not verify
is the one thing that actually breaks this gate: it ships code on your say-so
when you were guessing.

OUTPUT

One verdict per requirement, carrying that requirement's exact req_id, no
omissions and no inventions. An omitted requirement is treated as a failure,
so answer all of them. Each reason is one sentence naming the specific code,
file or search result that decided it -- a reason that could have been written
without looking at the code is not a reason."""


class RequirementVerdict(BaseModel):
    req_id: str = Field(description="the exact req_id this verdict answers")
    enforced: bool = Field(description="does the code enforce this requirement")
    determined: bool = Field(
        description="true when you established this from evidence you actually "
                    "read; false when a tool failed, a file was unreadable, or "
                    "you could not reach the evidence. Answering false is not a "
                    "failure -- it asks for another attempt. Guessing is worse.")
    confidence: int = Field(
        ge=0, le=100,
        description="0-100. How sure are you of this verdict, given the evidence "
                    "you actually read? Below 90 the verdict is discarded and the "
                    "requirement is asked again, so an honest low number costs "
                    "nothing and a dishonest high one decides whether code ships.")
    reason: str = Field(description="one sentence naming the deciding code")


class Audit(BaseModel):
    """One verdict per requirement, from one call.

    It was one call per requirement, which meant twelve per commit and around
    forty seconds of latency to answer twelve questions about one file. The
    model also saw each requirement alone, without the others for context.
    One call carrying the whole story reads better and costs a twelfth.
    """
    verdicts: list[RequirementVerdict] = Field(
        description="exactly one verdict per requirement given, no more, no fewer")


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

    @staticmethod
    def _code_only(source: str) -> str:
        """The file with every comment and docstring removed.

        Prose is not evidence. A docstring saying a check exists is exactly
        what drifts away from the code, and a comment claiming a role holds no
        delete grant is a claim about Atlas that this file cannot make true.
        Left in, they are hints the model can agree with instead of reading
        what executes. ast.unparse rebuilds the module from the syntax tree,
        which never carried the comments, and the docstrings are dropped on
        the way through.
        """
        tree = ast.parse(source)
        for node in ast.walk(tree):
            body = getattr(node, "body", None)
            if not isinstance(body, list) or not body:
                continue
            if not isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            first = body[0]
            if (isinstance(first, ast.Expr)
                    and isinstance(first.value, ast.Constant)
                    and isinstance(first.value.value, str)):
                body.pop(0)
                if not body:
                    body.append(ast.Pass())
        return ast.unparse(tree)

    @staticmethod
    def _override() -> tuple[set[str], str]:
        """Requirements the operator has explicitly waived, and why.

        Refusing is the default and needs no configuration. Waiving takes two
        deliberate acts: naming every requirement by its full req_id, and
        writing a reason. Neither alone does anything, a blanket value is not
        accepted, and every waiver is emitted as a violation record so it
        appears in the same place a failure would. There is no quiet skip.
        """
        import os
        raw = (os.environ.get(_OVERRIDE_IDS_ENV) or "").strip()
        reason = (os.environ.get(_OVERRIDE_REASON_ENV) or "").strip()
        if not raw or not reason:
            return set(), ""
        ids = {part.strip() for part in raw.split(",") if part.strip()}
        if any(token in ids for token in ("*", "ALL", "all")):
            return set(), ""
        return ids, reason

    def _story(self) -> tuple[dict, dict, dict]:
        """The epic, feature and story that own this requirement set, as the
        backlog holds them. Nothing here is composed: the descriptions are the
        operator's own words, and the auditor reads them rather than my
        summary of them."""
        backlog = json.loads(
            (self._repo_root() / _BACKLOG).read_text(encoding="utf-8"))
        for epic in backlog["epics"]["epic"]:
            for feature in epic.get("features", {}).get("feature", []):
                for story in feature.get("stories", {}).get("story", []):
                    if story.get("story_id") == _STORY_ID:
                        return epic, feature, story
        return {}, {}, {}

    def _context(self) -> str:
        """Why the story exists, in the words of the people who wrote it.

        Without this the auditor sees twelve sentences and no purpose, and
        reads each one as literally as it can. A requirement saying 'no other
        file does any part of it' means something different once you know the
        story says migration is the only path data takes to the front-end
        cluster and runs only when a human releases it.
        """
        epic, feature, story = self._story()
        if not story:
            return ""
        blank = "\n\n"
        return (
            f"EPIC {epic.get('epic_id', '')} - {epic.get('name', '')}{blank}"
            f"{epic.get('description', '')}{blank}"
            f"FEATURE {feature.get('feature_id', '')} - {feature.get('name', '')}{blank}"
            f"{feature.get('description', '')}{blank}"
            f"STORY {story.get('story_id', '')} - {story.get('name', '')}{blank}"
            f"{story.get('description', '')}"
        )

    def _requirements(self) -> list[tuple[str, str, str]]:
        """(req_id, name, text) for every requirement of the story."""
        _epic, _feature, story = self._story()
        if not story:
            return []
        return [(r["req_id"], r.get("name", ""), r["requirement"])
                for r in story["requirements"]["requirement"]]

    def _auditor(self) -> Agent:
        """An agent with tools, not a prompt with a file glued to it.

        The requirements it must answer are not all about one file. B-002 says
        nothing else writes to the serving database; B-009 says exactly one
        file migrates. Neither is answerable by reading data_migration.py, and
        a single-shot call that only ever saw that file could do nothing but
        guess -- which is why the cross-file check came out earlier instead of
        getting fixed. With a repository to search, the model can go and look.
        """
        if self._agent is None:
            repo_root = self._repo_root()
            agent = Agent(
                OpenAIModel(_MODEL_NAME,
                            provider=OpenAIProvider(api_key=self._api_key())),
                output_type=Audit,
                system_prompt=_SYSTEM_PROMPT,
            )

            def _inside(relative: str) -> Path:
                resolved = (repo_root / relative).resolve()
                if repo_root not in resolved.parents and resolved != repo_root:
                    raise _ch_exception()(
                        mode="security_violation",
                        component="ScanDataMigrationComplianceWorker",
                        message=f"{relative!r} resolves outside the repository")
                return resolved

            @agent.tool_plain
            def read_file(path: str) -> str:
                """Read any file in the repository, by repo-relative path."""
                try:
                    return _inside(path).read_text(encoding="utf-8")[:200_000]
                except (OSError, UnicodeDecodeError) as exc:
                    return f"unreadable: {type(exc).__name__}: {exc}"

            @agent.tool_plain
            def find_files(pattern: str) -> list[str]:
                """Repo-relative paths matching a glob, e.g. '**/*.py'."""
                return sorted(
                    str(hit.relative_to(repo_root)).replace("\\", "/")
                    for hit in repo_root.glob(pattern)
                    if hit.is_file() and ".git" not in hit.parts
                )[:400]

            @agent.tool_plain
            def search_repository(text: str, glob: str = "**/*.py") -> list[str]:
                """Every 'path:line: text' whose line contains `text`. This is
                how a claim about the whole repository gets checked instead of
                assumed."""
                hits = []
                for candidate in repo_root.glob(glob):
                    if not candidate.is_file() or ".git" in candidate.parts:
                        continue
                    if any(part in (".venv", "__pycache__", "node_modules",
                                    "build", "localBuild")
                           for part in candidate.parts):
                        continue
                    try:
                        lines = candidate.read_text(encoding="utf-8").splitlines()
                    except (OSError, UnicodeDecodeError):
                        continue
                    relative = str(candidate.relative_to(repo_root)).replace("\\", "/")
                    for number, line in enumerate(lines, 1):
                        if text in line:
                            hits.append(f"{relative}:{number}: {line.strip()[:200]}")
                            if len(hits) >= 200:
                                return hits
                return hits

            self._agent = agent
        return self._agent

    def _api_key(self) -> str:
        from dotenv import dotenv_values
        values = dotenv_values(self._repo_root() / ".env")
        key = values.get("OPENAI_API_KEY")
        if not key:
            raise _ch_exception()(
                mode="config_error",
                component="ScanDataMigrationComplianceWorker",
                message=("OPENAI_API_KEY absent from .env; "
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

    def _audit(self, source: str,
               requirements: list[tuple[str, str, str]]) -> dict[str, "RequirementVerdict"]:
        """One call, one verdict per requirement, keyed by req_id.

        The agent has the repository to search, so it is told the file under
        audit by path as well as by content -- some requirements are about
        what OTHER files do, and it has to go and look.
        """
        from chathealthy_lib.llm import run_llm_sync
        blank = "\n\n"
        settled: dict[str, RequirementVerdict] = {}
        outstanding = list(requirements)

        for round_number in range(1, _AUDIT_ROUNDS + 1):
            listing = blank.join(
                f"{req_id} - {name}\n{text}" for req_id, name, text in outstanding)
            retry_note = "" if round_number == 1 else (
                f"This is attempt {round_number}. The requirements below are the "
                f"ones left unresolved: either no verdict came back for them, or "
                f"you reported you could not determine them. The ones you did "
                f"settle are kept and are not being asked again. Use your tools "
                f"on these -- read the files, run the searches -- and if a tool "
                f"fails, try a different path or pattern before answering.{blank}")
            prompt = (
                f"WHY THIS STORY EXISTS -- read this before judging any "
                f"requirement. These are the operator's own words from the "
                f"backlog, not a summary:{blank}"
                f"{self._context()}{blank}"
                f"FILE UNDER AUDIT: {_MIGRATION_FILE}{blank}"
                f"{retry_note}"
                f"You may read or search any file in the repository with your "
                f"tools. Several requirements below are claims about the "
                f"repository as a whole rather than about this file; check those "
                f"by searching rather than by assuming.{blank}"
                f"REQUIREMENTS ({len(outstanding)}), answer every one:{blank}"
                f"{listing}{blank}"
                f"SOURCE OF {_MIGRATION_FILE}:{blank}{source}"
            )
            result = run_llm_sync(
                self._auditor(), prompt,
                call_site="scan_data_migration_compliance_worker.audit",
                provider="openai",
                server="pre-commit",
                component="ScanDataMigrationComplianceWorker",
            )
            wanted = {req_id for req_id, _n, _t in outstanding}
            for verdict in result.output.verdicts:
                if (verdict.req_id in wanted and verdict.determined
                        and verdict.confidence >= _MIN_CONFIDENCE):
                    settled[verdict.req_id] = verdict

            outstanding = [r for r in outstanding if r[0] not in settled]
            if not outstanding:
                break

        return settled

    def run(self) -> int:
        staged = {f.replace("\\", "/") for f in self.files}
        any_violations = False

        if _MIGRATION_FILE not in staged:
            return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

        path = self._repo_root() / _MIGRATION_FILE
        if not path.is_file():
            return EXIT_VIOLATIONS_FOUND if any_violations else EXIT_OK

        source = self._code_only(path.read_text(encoding="utf-8"))
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

        try:
            verdicts = self._audit(source, requirements)
        except Exception as exc:  # noqa: BLE001
            self._emit_violation(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=_RULE_ID,
                resource=_MIGRATION_FILE,
                message=(
                    f"{_STORY_ID} could not be audited "
                    f"({type(exc).__name__}: {str(exc)[:300]}); a file whose "
                    f"compliance is unestablished is not committed"),
            ))
            return EXIT_VIOLATIONS_FOUND

        waived, waiver_reason = self._override()
        for req_id, name, _text in requirements:
            verdict = verdicts.get(req_id)
            settled_and_failing = verdict is not None and not verdict.enforced
            if (verdict is None or settled_and_failing) and req_id in waived:
                self._emit_violation(ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=_RULE_ID,
                    resource=_MIGRATION_FILE,
                    message=(
                        f"{req_id} ({name}) WAIVED BY OPERATOR OVERRIDE. "
                        f"Reason given: {waiver_reason}. The requirement was "
                        f"not satisfied; it was set aside for this commit."),
                    severity="warning",
                ))
                continue
            if verdict is None:
                self._emit_violation(ViolationRecord(
                    enforcement_id=self.enforcement_id,
                    rule_id=_RULE_ID,
                    resource=_MIGRATION_FILE,
                    message=(
                        f"{req_id} ({name}) was not determined after "
                        f"{_AUDIT_ROUNDS} attempts; an unanswered requirement is "
                        f"not a satisfied one"),
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
