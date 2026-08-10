"""Rule-065 branch gate — EPIC-008-F-012.

Commits are permitted only on `dev`. qa and prod receive code through
promote_chathealthy.py, never through a commit.

The refusal path is exercised end to end: a temp repo is put on a non-dev
branch with a file staged, the worker runs against it, and the browser notice
is captured by standing in for `webbrowser.open_new` and fetching the URL the
worker hands it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import urllib.request
import uuid
from pathlib import Path

import pytest

import commit_authorization_worker as caw
from commit_authorization_worker import CommitAuthorizationWorker
from enforcement_worker import EXIT_OK, EXIT_VIOLATIONS_FOUND

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "FrontEndApplicationLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()

ENFORCEMENT_ID = "Rule-065-ENF-001"
REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE = (
    REPO_ROOT
    / "architecture"
    / "DevOpsBuildDeployAndEnvironmentManagement"
    / "TestData"
    / "TestcommitToBadBranch"
)
FIXTURE_REL = "architecture/DevOpsBuildDeployAndEnvironmentManagement/TestData/TestcommitToBadBranch"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


@pytest.fixture
def staged_repo(tmp_path):
    """A git repo with the fixture staged. Branch name is parameterized by
    the caller via the returned factory."""
    def _make(branch: str) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        _git(repo, "init")
        _git(repo, "checkout", "-b", branch)
        (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
        _git(repo, "add", "seed.txt")
        _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "-m", "base")
        target = repo / FIXTURE_REL
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(FIXTURE, target)
        _git(repo, "add", FIXTURE_REL)
        return repo
    return _make


@pytest.fixture(autouse=True)
def isolate_audit_log(tmp_path, monkeypatch):
    """Keep test verdicts out of the real audit log."""
    monkeypatch.setattr(caw, "_AUDIT_LOG_PATH", tmp_path / "audit.log")


@pytest.fixture
def captured_notice(monkeypatch):
    """Stand in for the browser: fetch the notice URL and keep the HTML."""
    captured = {"html": None, "url": None}

    def _fake_open(url):
        captured["url"] = url
        with urllib.request.urlopen(url, timeout=5) as resp:
            captured["html"] = resp.read().decode("utf-8")
        return True

    monkeypatch.setattr(caw.webbrowser, "open_new", _fake_open)
    monkeypatch.setattr(caw, "_NOTICE_GRACE_SECONDS", 0)
    return captured


@pytest.fixture
def operator_approves(monkeypatch):
    """Stand in for the operator clicking APPROVE, so the branch gate — which
    runs after authorization — is what decides the outcome."""
    monkeypatch.setattr(CommitAuthorizationWorker, "_is_real_interactive_shell",
                        lambda self: False)
    monkeypatch.setattr(CommitAuthorizationWorker, "_prompt_via_browser",
                        lambda self, action: EXIT_OK)


def test_commit_to_non_dev_branch_is_refused(
    staged_repo, captured_notice, operator_approves, monkeypatch
):
    repo = staged_repo("qa")
    monkeypatch.chdir(repo)

    worker = CommitAuthorizationWorker(ENFORCEMENT_ID)
    assert worker.run() == EXIT_VIOLATIONS_FOUND
    assert worker.violation_count == 1


def test_refusal_notice_names_branch_and_every_violating_file(
    staged_repo, captured_notice, operator_approves, monkeypatch
):
    repo = staged_repo("main")
    monkeypatch.chdir(repo)

    CommitAuthorizationWorker(ENFORCEMENT_ID).run()

    html = captured_notice["html"]
    assert html is not None, "no browser notice was served"
    assert "wrong branch" in html.lower()
    assert "main" in html
    assert FIXTURE_REL in html
    assert "1 violating file(s)" in html
    assert "promote_chathealthy.py" in html


def test_dev_branch_falls_through_to_authorization(staged_repo, monkeypatch):
    """On dev the branch gate is transparent — the existing authorization
    path runs and decides."""
    repo = staged_repo("dev")
    monkeypatch.chdir(repo)

    reached = {"value": False}

    def _fake_prompt(self, action):
        reached["value"] = True
        return EXIT_OK

    monkeypatch.setattr(CommitAuthorizationWorker, "_is_real_interactive_shell",
                        lambda self: False)
    monkeypatch.setattr(CommitAuthorizationWorker, "_prompt_via_browser", _fake_prompt)

    assert CommitAuthorizationWorker(ENFORCEMENT_ID).run() == EXIT_OK
    assert reached["value"] is True


def test_detached_head_is_refused(
    staged_repo, captured_notice, operator_approves, monkeypatch
):
    """rev-parse --abbrev-ref returns the literal 'HEAD' when detached; that
    is not 'dev', so the commit is refused."""
    repo = staged_repo("dev")
    monkeypatch.chdir(repo)
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(repo)).decode().strip()
    _git(repo, "checkout", head)

    assert CommitAuthorizationWorker(ENFORCEMENT_ID).run() == EXIT_VIOLATIONS_FOUND


# ─────────────────────────────────────────────────────────────────────────────
# Interactive end-to-end test. Blocks on a real operator click, so it is
# opt-in: CHATHEALTHY_INTERACTIVE_TEST=1 python -m pytest ... -k interactive -s
# ─────────────────────────────────────────────────────────────────────────────

MANAGER = REPO_ROOT / "architecture" / "EngineeringRuleEnforcement" / "code" / \
    "chathealthy_enforcement_manager.py"

_HOOK_BODY = """#!/bin/bash
echo ran > "{marker}"
python "{manager}" --hook pre-commit
exit $?
"""


@pytest.mark.skipif(
    os.environ.get("CHATHEALTHY_INTERACTIVE_TEST") != "1",
    reason="interactive: requires a real operator click on the authorization page",
)
def test_interactive_commit_to_qa_is_refused_after_authorization(tmp_path):
    """Random file, real git commit, real hook chain, real authorization page.

    Expected: the authorization page opens and the operator APPROVES; the
    branch gate then refuses because the branch is qa. A commit that succeeds
    is a failed test.
    """
    repo = tmp_path / "qa_repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "checkout", "-b", "qa")
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(repo, "add", "seed.txt")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-m", "base")

    marker = repo / "hook_ran.marker"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        _HOOK_BODY.format(manager=MANAGER.as_posix(), marker=marker.as_posix()),
        encoding="utf-8",
        newline="\n",
    )
    hook.chmod(0o755)

    random_name = f"random_{uuid.uuid4().hex[:12]}.txt"
    (repo / random_name).write_text(uuid.uuid4().hex + "\n", encoding="utf-8")
    _git(repo, "add", random_name)

    _CH_LOG.info(f"\n[interactive] branch=qa  staged file={random_name}")
    _CH_LOG.info("[interactive] APPROVE on the page that opens; the commit must still fail.")

    result = subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "should not land"],
        cwd=str(repo), capture_output=True, text=True, timeout=900,
    )
    _CH_LOG.info("[interactive] git exit:", result.returncode)
    _CH_LOG.info(result.stdout)
    _CH_LOG.info(result.stderr)
    combined = result.stdout + result.stderr

    assert marker.exists(), "the pre-commit hook never ran"

    assert "Authorization requested at:" in combined, \
        "no authorization page was served — you were never asked to approve"

    if result.returncode == 0:
        landed = subprocess.check_output(
            ["git", "log", "-1", "--format=%H %s"], cwd=str(repo)
        ).decode().strip()
        assert False, (
            f"commit to qa SUCCEEDED and must not have. HEAD is now: {landed}"
        )

    assert "Commit refused. Details at:" in combined, \
        "commit was refused but no violation page was served"

    assert "Refusal notice DISPLAYED." in combined, \
        ("the violation page was served but no browser ever fetched it — "
         "you did not get the popup")

    assert "Rule-065" in combined, \
        "commit failed, but not via Rule-065 — check which enforcement blocked it"
