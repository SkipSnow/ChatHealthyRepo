"""The chain itself comes from the branch it is deploying.

A cloud build reads its source from origin/<branch> and never the working
tree, so the artifact is always the reviewed commit. The programs doing the
reading were exempt from their own rule: build_chathealthy.py,
deploy_chathealthy.py, _deploy_chain.py and every helper they import execute
from whatever is on disk, because that is the file the operator invoked.

So an uncommitted edit to the chain changed how a deployment was produced
while the deployment reported the approved commit. Build 2210 is the worked
example: its secret gate ran with an uncommitted fix and packaged
19761325, and both facts were true at once. What governs a deployment was
outside what governs deployments.

Local is exempt. --env local builds from the working tree on purpose, and a
chain required to match origin there could never be used to test a change to
itself.

Imported-only. No entry point.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import sys as _ch_sys
import pathlib as _ch_pl

for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

CHAIN_DIRECTORY = "architecture/DevOpsBuildDeployAndEnvironmentManagement"


def _git(repo_root: Path, *argv: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", *argv], cwd=str(repo_root),
        capture_output=True, text=True, shell=False)
    return completed.returncode, ((completed.stdout or "")
                                  + (completed.stderr or "")).strip()


ENV_MARKER = "CHATHEALTHY_CHAIN_SOURCE"
ENV_CANONICAL = "CHATHEALTHY_CANONICAL_REPO"
# Unix time at which the operator's invocation began, set by the parent before
# it fetches and checks out. The re-executed child starts its clocks from this
# rather than from its own import, so the fetch and checkout -- the first thing
# the operator waits for -- are inside the measurement and not before it.
ENV_STARTED = "CHATHEALTHY_CHAIN_STARTED"


def chain_started_at() -> float:
    """When this invocation began, as Unix time. The parent's start when this
    is a re-executed child; this process's start otherwise."""
    import os  # noqa: PLC0415
    import time  # noqa: PLC0415
    raw = os.environ.get(ENV_STARTED, "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    started = time.time()
    os.environ[ENV_STARTED] = repr(started)
    return started
_ENV_BRANCH = {"dev": "dev", "qa": "qa", "prod": "main"}


def canonical_repo_override() -> Path | None:
    """The real repository, when this process is running out of a checkout.

    The chain's helpers find the repository by walking up for .git, which in
    a re-executed run lands on the checkout. Almost everything should read
    from the checkout -- that is the point -- but three things must not:
    .env is not in git, the build output directory belongs to the
    workstation, and the checkout is deleted when the run ends. Those read
    the canonical repository the parent passed down.
    """
    import os  # noqa: PLC0415

    value = os.environ.get(ENV_CANONICAL, "").strip()
    return Path(value) if value else None


def repository_root(start: Path | None = None) -> Path:
    """The repository this run belongs to. One answer, for every caller.

    Six modules in the chain each walked up for a root and each disagreed
    about what marks one -- .git as a directory, .git by any kind, .git or a
    .env beside it. Running from a checkout made every one of those
    differences matter at once: a git worktree carries .git as a FILE, and a
    checkout carries no .env at all, so the walks that tested is_dir() or
    looked for .env ran off the top of the filesystem.

    The override wins when set, because in a re-executed run the thing being
    asked for is the workstation's repository -- where .env lives and where
    build output belongs -- and not the checkout the code is running from.
    """
    override = canonical_repo_override()
    if override is not None:
        return override
    current = (start or Path(__file__)).resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return candidate
    raise ChatHealthyException(
        mode="repo_root_not_found",
        component="chain_provenance",
        message=f"no .git found walking up from {current}")


class ChainTimeline:
    """Every logged step of a build or deploy, with when it happened.

    The chain used to log free text with a wall-clock timestamp and persist
    nothing about its own duration; a question such as "where did the fifteen
    minutes go" could only be answered by reading a saved terminal with a
    stopwatch, and the terminals were not saved. Each step is recorded here
    with its offset from the invocation's start and the gap since the previous
    step; the end of a run prints the table and hands the record to whoever
    persists it.
    """

    def __init__(self, started_at: float) -> None:
        import threading  # noqa: PLC0415
        self.started_at = started_at
        self.steps: list[dict] = []
        self._lock = threading.Lock()
        self._last = started_at

    def mark(self, source: str, message: str) -> tuple[float, float]:
        import time  # noqa: PLC0415
        now = time.time()
        with self._lock:
            delta = now - self._last
            self._last = now
            self.steps.append({
                "t": round(now - self.started_at, 3),
                "delta": round(delta, 3),
                "source": source,
                "message": message,
            })
        return delta, now - self.started_at

    def elapsed(self) -> float:
        import time  # noqa: PLC0415
        return time.time() - self.started_at

    def slowest(self, n: int = 10) -> list[dict]:
        """The n steps whose gap from the previous step was largest -- the gap
        is the cost of the work that ended at that step."""
        return sorted(self.steps, key=lambda s: s["delta"], reverse=True)[:n]

    def summary_lines(self, title: str) -> list[str]:
        total = self.elapsed()
        lines = [f"{title}: {total:.1f}s over {len(self.steps)} step(s); "
                 f"slowest steps (gap = time spent reaching that line):"]
        for s in self.slowest():
            if s["delta"] < 0.5:
                continue
            pct = 100.0 * s["delta"] / total if total else 0.0
            lines.append(f"  {s['delta']:6.1f}s {pct:4.0f}%  [{s['source']}] {s['message'][:110]}")
        return lines

    def to_doc(self, outcome: str) -> dict:
        from datetime import datetime, timezone  # noqa: PLC0415
        return {
            "started_at": datetime.fromtimestamp(self.started_at, tz=timezone.utc),
            "finished_at": datetime.now(timezone.utc),
            "duration_s": round(self.elapsed(), 1),
            "outcome": outcome,
            "steps": list(self.steps),
        }


_TIMELINE: list[ChainTimeline] = []


def timeline() -> ChainTimeline:
    """The one timeline for this process, started at the invocation's start."""
    if not _TIMELINE:
        _TIMELINE.append(ChainTimeline(chain_started_at()))
    return _TIMELINE[0]


def running_from_git() -> str:
    import os  # noqa: PLC0415

    return os.environ.get(ENV_MARKER, "").strip()


def reexec_from_branch(env: str, entry_relative_path: str,
                       argv: list[str]) -> int | None:
    """Re-run this program out of a fresh checkout of origin/<branch>.

    Returns the child's exit code, which the entry function returns in turn.
    Returns None when this process should carry on -- for --env local, whose
    whole purpose is to run what the operator has on disk, and for a process
    that is already the child.

    Checking that the chain matches the branch was the weaker version of
    this and is not enough: a check is only in force where somebody
    remembered to call it, and the thing being checked is the code doing the
    remembering. Running from the checkout removes the question. A local
    edit to the chain cannot influence a cloud deployment, because a local
    edit is not what runs.
    """
    import os  # noqa: PLC0415
    import shutil  # noqa: PLC0415
    import sys  # noqa: PLC0415
    import tempfile  # noqa: PLC0415

    if env == "local" or running_from_git():
        return None
    branch = _ENV_BRANCH.get(env)
    if branch is None:
        raise ChatHealthyException(
            mode="value_error",
            component="chain_provenance",
            message=f"env {env!r} names no branch, so there is no checkout to "
                    f"run the chain out of")

    canonical = Path(__file__).resolve()
    while canonical != canonical.parent and not (canonical / ".git").exists():
        canonical = canonical.parent

    started = chain_started_at()
    _log.info(f"[chain] fetching origin/{branch}")
    code, out = _git(canonical, "fetch", "origin", branch)
    if code != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"origin/{branch} could not be fetched, so the chain has "
                    f"no checkout to run out of: {out[:300]}")
    code, head = _git(canonical, "rev-parse", "--short", f"origin/{branch}")
    if code != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"origin/{branch} could not be resolved: {head[:300]}")

    checkout = Path(tempfile.mkdtemp(prefix=f"chain_{env}_{branch}_"))
    code, out = _git(canonical, "worktree", "add", "--detach", "--force",
                     str(checkout), f"origin/{branch}")
    if code != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"origin/{branch} could not be checked out at {checkout}: "
                    f"{out[:300]}")

    entry = checkout / entry_relative_path
    if not entry.is_file():
        _git(canonical, "worktree", "remove", "--force", str(checkout))
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"origin/{branch} carries no {entry_relative_path}, so the "
                    f"chain cannot be run from the branch it is deploying")

    child_env = dict(os.environ)
    child_env[ENV_MARKER] = head
    child_env[ENV_CANONICAL] = str(canonical)
    child_env[ENV_STARTED] = repr(started)
    import time as _time  # noqa: PLC0415
    _log.info(f"[chain] running from origin/{branch} at {head} ({checkout}); "
              f"fetch+checkout took {_time.time() - started:.1f}s")
    try:
        completed = subprocess.run(
            [sys.executable, str(entry), *argv],
            env=child_env, cwd=str(canonical), shell=False)
        return completed.returncode
    finally:
        _git(canonical, "worktree", "remove", "--force", str(checkout))
        shutil.rmtree(checkout, ignore_errors=True)


def require_chain_matches_branch(repo_root: Path, env: str,
                                 branch: str | None = None) -> None:
    """Refuse a cloud run when the chain on disk differs from origin/<branch>.

    Compares the chain directory against the branch the environment deploys
    from. Any difference -- modified, staged, added or deleted -- stops the
    run and names the files, because which of them is harmless is not a
    judgment this code can make.
    """
    if env == "local":
        return
    target_branch = branch or {"dev": "dev", "qa": "qa", "prod": "main"}.get(env)
    if not target_branch:
        raise ChatHealthyException(
            mode="value_error",
            component="chain_provenance",
            message=f"env {env!r} names no branch, so the chain has nothing to "
                    f"be checked against")

    code, _ = _git(repo_root, "fetch", "origin", target_branch, "--quiet")
    if code != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"origin/{target_branch} could not be fetched, so whether "
                    f"this chain matches the branch it is deploying is unknown")

    code, out = _git(repo_root, "diff", "--name-only",
                     f"origin/{target_branch}", "--", CHAIN_DIRECTORY)
    if code != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"the chain could not be compared to origin/"
                    f"{target_branch}: {out[:400]}")

    differing = [line for line in out.splitlines() if line.strip()]
    if differing:
        listed = ", ".join(differing[:12])
        more = "" if len(differing) <= 12 else f" (and {len(differing) - 12} more)"
        raise ChatHealthyException(
            mode="aborted",
            component="chain_provenance",
            message=f"{len(differing)} file(s) in {CHAIN_DIRECTORY} differ from "
                    f"origin/{target_branch}, so this run would be produced by "
                    f"code that is not on the branch it is deploying: {listed}"
                    f"{more}. Commit and push them, or run --env local.")
    _log.info(f"[chain] matches origin/{target_branch}")
