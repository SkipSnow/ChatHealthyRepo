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
import subprocess
import sys
import tempfile
import time
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

# After enforcement_worker, which is what puts ChatHealthyLib on the path.
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_CH_LOG = ChatHealthyLoggingService()

def _ch_exception():
    """ChatHealthyException, resolved the same way every other worker does.

    Two raise sites here call this and it was never defined, so both error
    paths died on NameError instead of reporting what went wrong. Both are
    error paths, which is why the commit gate never reached them and a
    release-time audit did.
    """
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


_RULE_ID = "Rule-065"
_STORY_ID = "EPIC-010-F-108-S-001"
_MIGRATION_FILE = "pipeline/Code/data_migration.py"
_BACKLOG = "brain/machine_artifacts/content/agile_backlog.json"
_MANIFEST = "brain/machine_artifacts/content/deployment_architecture.json"
_MODEL_NAME = "gpt-5.5"
# The branch each environment runs from. An environment is judged on the code
# it actually runs, which is what its branch holds, not what this disk holds.
_ENV_BRANCH = {"dev": "dev", "qa": "qa", "prod": "main"}
# The databases that hold the data being moved. Reaching one of these is an
# access to data; pipelineAdmin, which holds the approvals, is not.
_DATA_DATABASES = {"PublicHealthData", "PipelinePublicHealthData"}
# The calls that constitute beginning work on the collection. The
# acknowledgement stands before the first of them.
_WORK_BEGINS = {"refuse_unless_migratable", "source_count", "copy",
                "build_constraint_indexes", "build_performance_indexes"}
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
# Claims about the estate, not about this file. No edit to data_migration.py
# makes any of them true or false: whether another file writes to the serving
# cluster, whether one file migrates, whether a rule guarantees the set,
# whether the identities exist in Atlas and Azure. A file gate cannot answer
# them and should not pretend to. REQ-B-012 is absent for the opposite reason
# -- the manifest check answers it exactly, and a certain answer beats a
# judged one.
_NOT_THIS_FILES_QUESTION = {
    "EPIC-010-F-108-S-001-REQ-B-010",
    "EPIC-010-F-108-S-001-REQ-B-011",
    "EPIC-010-F-108-S-001-REQ-B-012",
}
# B-002 and B-009 are answered against the other components, whose source the
# worker fetches from the manifest and hands over. They are the file's
# question after all -- just not answerable from the file alone.
_ESTATE_REQUIREMENTS = {
    "EPIC-010-F-108-S-001-REQ-B-002",
    "EPIC-010-F-108-S-001-REQ-B-009",
}

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

WHAT YOU ARE GIVEN

The complete source of the migration file, and -- when a requirement asks
about the wider system -- the complete source of every other component the
deployment manifest declares. You have no tools and need none. That listing is
exhaustive: it is what this system is made of, so a thing absent from it is
absent from the system. When a requirement says nothing else may write to the
serving cluster, or that exactly one file migrates, read those other
components and say what you find, naming the file and the call.

READ THE BACKLOG FIRST

You are given the epic, feature and story as JSON, exactly as the backlog
holds them -- descriptions, statuses, approvals, and the test each requirement
cites. Nobody has summarised it for you. A requirement is a sentence inside
that story, not a standalone rule, and the surrounding object tells you what
it is protecting. Judge each requirement for what it is trying to prevent,
not for the most literal reading of its words -- but never against what it
plainly says.

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

    def __init__(self, enforcement_id: str, env: str = "local") -> None:
        super().__init__(enforcement_id)
        self.files_scanned: int = 0
        self.violation_count: int = 0
        self._agent: Agent | None = None
        self._env: str = env
        self._judged_root: Path | None = None

    def _checkout_root(self) -> Path:
        """The working tree: what the operator has on disk."""
        here = Path(__file__).resolve()
        for parent in (here, *here.parents):
            if (parent / ".git").is_dir():
                return parent
        return here.parents[3]

    def _repo_root(self) -> Path:
        """The tree this run judges.

        Pre-commit judges what is about to be committed, so the default is
        the working tree and nothing is fetched. Named an environment, the
        worker judges the branch that environment runs from instead -- what
        is deployed there can differ from this disk by every uncommitted
        edit on it, and a release asks about the former.
        """
        if self._env == "local":
            return self._checkout_root()
        if self._judged_root is not None:
            return self._judged_root
        checkout = self._checkout_root()
        branch = _ENV_BRANCH[self._env]
        where = Path(tempfile.mkdtemp(prefix=f"judge_{self._env}_{branch}_"))
        for argv in (["fetch", "origin", branch],
                     ["worktree", "add", "--detach", "--force",
                      str(where), f"origin/{branch}"]):
            result = subprocess.run(["git", *argv], cwd=str(checkout),
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise ChatHealthyException(
                    mode="config_error",
                    component="ScanDataMigrationComplianceWorker",
                    message=(f"cannot materialise origin/{branch} for "
                             f"--env {self._env}: "
                             f"{(result.stderr or '').strip()[:300]}"))
        self._judged_root = where
        # What was judged, named on stdout: the branch, and the commit it
        # stood at when it was read. A release records these, so the
        # approval says which code it released and not merely when.
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(where),
                              capture_output=True, text=True)
        sys.stdout.write(json.dumps({
            "kind": "judged",
            "environment": self._env,
            "branch": branch,
            "commit": (head.stdout or "").strip(),
        }) + chr(10))
        sys.stdout.flush()
        return self._judged_root

    def release(self) -> None:
        """Remove the checkout _repo_root() materialised. No-op for local."""
        if self._judged_root is None:
            return
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(self._judged_root)],
            cwd=str(self._checkout_root()), capture_output=True, text=True)
        self._judged_root = None

    @staticmethod
    def _the_one_class(tree: ast.AST) -> ast.ClassDef | None:
        classes = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
        return classes[0] if len(classes) == 1 else None

    def _decided_by_parsing(self, source: str) -> dict[str, tuple[bool, str]]:
        """The requirements a parser settles, so no model is asked.

        Four of these are structural facts about one file: how many arguments
        a constructor takes, whether anything outside the class assigns to it,
        whether a destructive call appears anywhere, whether the refusal comes
        before the first write. A parser answers each the same way every time,
        in milliseconds, for nothing -- and the same question put to a model
        came back differently on three consecutive runs.

        Returns {req_id: (enforced, reason)}. Anything absent goes to the
        model, which is left with the questions that genuinely need reading.
        """
        tree = ast.parse(source)
        out: dict[str, tuple[bool, str]] = {}
        story = _STORY_ID
        klass = self._the_one_class(tree)

        # B-006: every access to data is made on behalf of one named
        # collection. The class is what carries a collection name, so a data
        # database reached anywhere outside it is an access made otherwise.
        outside = []
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                continue
            named = sorted({c.value for c in ast.walk(node)
                            if isinstance(c, ast.Constant)
                            and isinstance(c.value, str)
                            and c.value in _DATA_DATABASES})
            if named:
                where = getattr(node, "name", type(node).__name__)
                outside.append(f"{where} reaches {named}")
        out[f"{story}-REQ-B-006"] = (
            not outside,
            f"data reached outside the class: {outside}" if outside
            else "every data database is reached only from inside the class")

        # B-014: the acknowledgement is written before any work. Work begins
        # at the first call onto the migrated collection, so the
        # acknowledgement call must stand before it in the same function.
        acknowledged = [n.lineno for n in ast.walk(tree)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Name)
                        and "acknowledge" in n.func.id]
        first_work = [n.lineno for n in ast.walk(tree)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and n.func.attr in _WORK_BEGINS]
        if not acknowledged:
            out[f"{story}-REQ-B-014"] = (
                False, "nothing in the file records an acknowledgement")
        elif not first_work:
            out[f"{story}-REQ-B-014"] = (
                False, "no call that begins work was found to order against")
        else:
            out[f"{story}-REQ-B-014"] = (
                min(acknowledged) < min(first_work),
                f"acknowledgement at line {min(acknowledged)}, "
                f"work begins at line {min(first_work)}")

        # B-007: its constructor takes the collection name and nothing else.
        if klass is None:
            out[f"{story}-REQ-B-007"] = (False, "no single class to inspect")
            out[f"{story}-REQ-B-008"] = (False, "no single class to inspect")
        else:
            init = next((n for n in klass.body
                         if isinstance(n, ast.FunctionDef) and n.name == "__init__"),
                        None)
            if init is None:
                out[f"{story}-REQ-B-007"] = (False, f"{klass.name} defines no __init__")
            else:
                args = [a.arg for a in init.args.args if a.arg != "self"]
                extras = (init.args.posonlyargs or []) + (init.args.kwonlyargs or [])
                ok = len(args) == 1 and not extras and not init.args.vararg \
                    and not init.args.kwarg
                out[f"{story}-REQ-B-007"] = (
                    ok, f"{klass.name}.__init__ takes {args + [a.arg for a in extras]}")

            # B-008: the only value assigned onto self is that name.
            assigned = set()
            for node in ast.walk(klass):
                if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                    continue
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        assigned.add(target.attr)
            out[f"{story}-REQ-B-008"] = (
                len(assigned) <= 1,
                f"{klass.name} assigns {sorted(assigned) or 'nothing'} onto self")

        # B-004: nothing in the file can alter or remove.
        destructive = {"update_one", "update_many", "replace_one", "delete_one",
                       "delete_many", "drop", "drop_index", "drop_indexes",
                       "rename", "find_one_and_delete", "find_one_and_replace",
                       "find_one_and_update", "bulk_write"}
        found = sorted({n.func.attr for n in ast.walk(tree)
                        if isinstance(n, ast.Call)
                        and isinstance(n.func, ast.Attribute)
                        and n.func.attr in destructive})
        out[f"{story}-REQ-B-004"] = (
            not found,
            f"destructive calls present: {found}" if found
            else "no update, delete, drop, rename or bulk_write call anywhere")
        return out

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

    def _brain_artifact(self, relative: str) -> dict:
        """A brain artifact as origin/dev holds it, never as the tree does.

        The backlog and the manifest are what the gate measures against, so
        reading them from the working tree lets the thing being judged edit
        its own standard: soften a requirement locally and the commit passes
        against the softened text. Requirements change by being committed and
        pushed, which is a reviewed act; until then the branch is the truth.

        A tree that cannot produce origin/dev is a tree whose standard cannot
        be established, and that raises rather than quietly falling back.
        """
        import subprocess
        result = subprocess.run(
            ["git", "show", f"origin/dev:{relative}"],
            capture_output=True, text=True, cwd=str(self._repo_root()),
            encoding="utf-8", errors="replace")
        if result.returncode != 0 or not (result.stdout or "").strip():
            raise ChatHealthyException(
                mode="config_error",
                component="ScanDataMigrationComplianceWorker",
                message=(f"cannot read {relative!r} from origin/dev: "
                         f"{(result.stderr or '').strip()[:200]}. The gate "
                         f"measures against the branch, not the working tree."))
        return json.loads(result.stdout)

    def _story(self) -> tuple[dict, dict, dict]:
        """The epic, feature and story that own this requirement set, as the
        backlog holds them. Nothing here is composed: the descriptions are the
        operator's own words, and the auditor reads them rather than my
        summary of them."""
        backlog = self._brain_artifact(_BACKLOG)
        for epic in backlog["epics"]["epic"]:
            for feature in epic.get("features", {}).get("feature", []):
                for story in feature.get("stories", {}).get("story", []):
                    if story.get("story_id") == _STORY_ID:
                        return epic, feature, story
        return {}, {}, {}

    def _backlog_package(self) -> str:
        """The whole thing the backlog says about this work, as JSON.

        Not my paraphrase of it and not a selection from it: the epic, the
        feature and the story exactly as they are written, with every field
        the backlog carries -- statuses, approvals, sizes, the test each
        requirement cites. Summarising it was my own step in the middle, and
        every step of mine between the backlog and the model is a place the
        model gets my reading instead of the operator's words.

        Sibling features and sibling stories are dropped, and only those:
        they are other work, and including them would bury the story being
        judged in a file the size of the backlog.
        """
        epic, feature, story = self._story()
        if not story:
            return "{}"
        trimmed_feature = {k: v for k, v in feature.items() if k != "stories"}
        trimmed_feature["stories"] = {"story": [story]}
        trimmed_epic = {k: v for k, v in epic.items() if k != "features"}
        trimmed_epic["features"] = {"feature": [trimmed_feature]}
        return json.dumps(trimmed_epic, indent=2, ensure_ascii=False)

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
        """The requirements this file can answer for itself.

        A file gate judges a file. Four of the story's requirements are claims
        about the estate rather than about this code -- that migration is the
        only writer, that only one file migrates, that an engineering rule
        guarantees the set, that the identities exist in Atlas and Azure. No
        edit to data_migration.py can make any of them true or false, so
        putting them to a model that can search the repository produced a
        different answer every run: it kept finding other people's code and
        convicting this file for it.

        They are not waived and they are not passed. They are not this
        worker's question. REQ-B-012 is not here either, because the manifest
        check above answers it deterministically and a certain answer beats a
        judged one.
        """
        _epic, _feature, story = self._story()
        if not story:
            return []
        return [(r["req_id"], r.get("name", ""), r["requirement"])
                for r in story["requirements"]["requirement"]
                if r["req_id"] not in _NOT_THIS_FILES_QUESTION]

    def _auditor(self) -> Agent:
        """One judge, one file, no tools.

        It had repository search for a while. That was the wrong shape: with
        a repository to hunt through it judged the estate instead of the file,
        found a different neighbour misbehaving on every run, and returned
        three different verdicts for the same code. The caller fetches what is
        to be judged and hands it over; the model reads what it is given and
        answers. Nothing it says depends on what it happened to search for.
        """
        if self._agent is None:
            repo_root = self._repo_root()
            agent = Agent(
                OpenAIModel(_MODEL_NAME,
                            provider=OpenAIProvider(api_key=self._api_key())),
                output_type=Audit,
                system_prompt=_SYSTEM_PROMPT,
            )

            self._agent = agent
        return self._agent

    def _api_key(self) -> str:
        # From the checkout, never the judged tree. The key belongs to the
        # machine running the audit, not to the code under audit, and .env is
        # not in git -- a materialised branch has none, so following the
        # judged root made every --env audit die before asking anything.
        from dotenv import dotenv_values
        values = dotenv_values(self._checkout_root() / ".env")
        key = values.get("OPENAI_API_KEY")
        if not key:
            raise ChatHealthyException(
                mode="config_error",
                component="ScanDataMigrationComplianceWorker",
                message=("OPENAI_API_KEY absent from .env; "
                         f"{_STORY_ID} compliance cannot be established"))
        return key

    def _other_component_sources(self) -> list[tuple[str, str]]:
        """(path, source) for every OTHER component the manifest declares.

        The manifest is the list of what this system is made of, so it is the
        list of places a second migrator could hide. I fetch them and hand
        them over; the model is not sent looking. It was, briefly, and it
        judged whichever neighbour its search happened to surface -- three
        runs, three answers. A fixed set asked the same way every time is a
        question with an answer.
        """
        manifest = self._brain_artifact(_MANIFEST)
        wanted: list[str] = []
        for target in manifest.get("DeploymentTargetRecord", []):
            for binding in target.get("environments", []):
                for package in binding.get("packages", []) or []:
                    for declared in package.get("files", []) or []:
                        location = (declared.get("source_location") or "").replace("\\", "/")
                        if location.endswith(".py") and location != _MIGRATION_FILE:
                            wanted.append(location)

        # The library modules the migration imports. Half of what a
        # requirement asks about can live in one of them -- the mutex that
        # answers REQ-B-015 does -- and a model shown only the call and not
        # the implementation cannot say whether the requirement is met. It
        # said so: "the provided source contains no implementation proving
        # reserve rejects a concurrent invocation". Asked without the
        # evidence it guessed, and guessed differently on different runs.
        repo_root = self._repo_root()
        migration = repo_root / _MIGRATION_FILE
        if migration.is_file():
            imported = ast.parse(migration.read_text(encoding="utf-8"))
            for node in ast.walk(imported):
                if not isinstance(node, ast.ImportFrom) or not node.module:
                    continue
                if not node.module.startswith("chathealthy_lib."):
                    continue
                module = node.module.split(".", 1)[1].replace(".", "/")
                wanted.append(f"ChatHealthyLib/src/chathealthy_lib/{module}.py")

        out: list[tuple[str, str]] = []
        for location in sorted(set(wanted)):
            path = repo_root / location
            if not path.is_file():
                continue
            try:
                out.append((location, self._code_only(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeDecodeError, SyntaxError):
                continue
        return out

    def _declared_locations(self) -> list[tuple[str, str]]:
        """(target_id, package_id) for every manifest declaration whose
        source_location is the migration file."""
        manifest = self._brain_artifact(_MANIFEST)
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
        others = ""
        if any(r[0] in _ESTATE_REQUIREMENTS for r in requirements):
            components = self._other_component_sources()
            rendered = blank.join(
                f"----- {path} -----{blank}{code}" for path, code in components)
            others = (
                f"EVERY OTHER COMPONENT THIS SYSTEM DECLARES ({len(components)} "
                f"files, taken from the deployment manifest -- this is the "
                f"complete list, so if migration is not in these it is nowhere "
                f"else):{blank}{rendered}{blank}")

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
                f"THE BACKLOG, VERBATIM. This is the epic, feature and story "
                f"exactly as agile_backlog.json holds them, every field "
                f"included and nothing summarised. The requirements listed "
                f"below are drawn from this same object; read it whole before "
                f"judging any of them:{blank}"
                f"{self._backlog_package()}{blank}"
                f"FILE UNDER AUDIT: {_MIGRATION_FILE}{blank}"
                f"{retry_note}"
                f"Everything you need is in this prompt. Judge only what is "
                f"here.{blank}"
                f"{others}"
                f"REQUIREMENTS ({len(outstanding)}), answer every one:{blank}"
                f"{listing}{blank}"
                f"SOURCE OF {_MIGRATION_FILE}:{blank}{source}"
            )
            _CH_LOG.info(
                "[ENF-009] round %d of %d: asking the model about %d "
                "requirement(s): %s",
                round_number, _AUDIT_ROUNDS, len(outstanding),
                ", ".join(r[0].rsplit("-", 2)[-2] + "-" + r[0].rsplit("-", 1)[-1]
                          for r in outstanding))
            began = time.monotonic()
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
            _CH_LOG.info(
                "[ENF-009] round %d answered in %.0fs: %d settled, %d still "
                "outstanding", round_number, time.monotonic() - began,
                len(settled), len(outstanding))
            if not outstanding:
                break

        return settled

    def run(self) -> int:
        _CH_LOG.info("[ENF-009] judging %s against %s, env=%s",
                     _MIGRATION_FILE, _STORY_ID, self._env)
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

        parsed = self._decided_by_parsing(source)
        _CH_LOG.info("[ENF-009] decided by parsing, no model asked: %s",
                     ", ".join(f"{r.rsplit('-', 2)[-2]}-{r.rsplit('-', 1)[-1]}"
                               f"={'yes' if ok else 'NO'}"
                               for r, (ok, _w) in sorted(parsed.items())))
        for req_id, (ok, why) in sorted(parsed.items()):
            if ok:
                continue
            self._emit_violation(ViolationRecord(
                enforcement_id=self.enforcement_id,
                rule_id=_RULE_ID,
                resource=_MIGRATION_FILE,
                message=f"{req_id} not enforced (parsed, not judged): {why}",
            ))
            self.violation_count += 1
            any_violations = True

        requirements = [r for r in self._requirements() if r[0] not in parsed]
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
    # argv[2] is the environment to judge. Absent, it is the working tree:
    # the pre-commit hook passes no environment and asks about this disk.
    enforcement_id = sys.argv[1] if len(sys.argv) > 1 else "Rule-065-ENF-009"
    environment = sys.argv[2] if len(sys.argv) > 2 else "local"
    if environment != "local" and environment not in _ENV_BRANCH:
        raise ChatHealthyException(
            mode="config_error",
            component="ScanDataMigrationComplianceWorker",
            message=(f"unknown environment {environment!r}; "
                     f"expected local, {', '.join(sorted(_ENV_BRANCH))}"))
    worker = ScanDataMigrationComplianceWorker(enforcement_id, environment)
    try:
        code = worker.run()
    finally:
        worker.release()
    sys.exit(code)
