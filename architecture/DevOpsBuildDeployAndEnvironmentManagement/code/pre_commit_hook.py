"""Pre-commit gate: load the brain artifact + backlog, run Crosswalk,
print reject lines on failure, return an exit code.

The gate is pure: it does NOT auto-extract, does NOT mutate the working
tree, does NOT touch the index. Reject = hard stop; the human resolves
the drift, then re-stages and re-commits.
"""
from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

from agile_backlog import AgileBacklogLoader
from crosswalk import Crosswalk
from record_loader import RecordLoader
from secrets_resolver import SecretsResolver


def _install_browser_ua_opener() -> None:
    """Install an urllib opener that sends a browser User-Agent.

    The schema is served behind an edge that rejects the default
    `Python-urllib/...` User-Agent with 403. Setting a browser UA is a
    transport detail, not a fallback (we still fetch over HTTPS from
    the canonical URL and refuse on any non-200).
    """
    opener = urllib.request.build_opener()
    opener.addheaders = [
        (
            "User-Agent",
            "Mozilla/5.0 (deployment-substrate-loader; +chathealthy.ai)",
        )
    ]
    urllib.request.install_opener(opener)


_BACKLOG_REL: str = "brain/machine_artifacts/content/agile_backlog.json"
_DEPLOYMENT_REL: str = "brain/machine_artifacts/content/deployment_architecture.json"
_BACKLOG_SCHEMA_REL: str = "Website/schemas/ChatHealthyAgileBacklogSchema.json"
_ENV_REL: str = "Code/.env"


class PreCommitHook:
    def __init__(self, repo_root: Path) -> None:
        self.repo_root: Path = repo_root.resolve()

    def on_commit(self, staged_files: list[Path]) -> int:
        backlog_path = self.repo_root / _BACKLOG_REL
        backlog_schema = self.repo_root / _BACKLOG_SCHEMA_REL
        deployment_path = self.repo_root / _DEPLOYMENT_REL
        env_path = self.repo_root / _ENV_REL

        _install_browser_ua_opener()
        backlog = AgileBacklogLoader(schema_uri=backlog_schema).load(backlog_path)
        coll = RecordLoader().load_collection(deployment_path)
        env_values: set[str] = set()
        if env_path.is_file():
            env_values = SecretsResolver().env_values_for_leak_check(env_path)

        report = Crosswalk().check(
            coll=coll,
            backlog=backlog,
            repo_root=self.repo_root,
            env_values=env_values,
        )
        if not report.is_pass:
            sys.stderr.write(report.format() + "\n")
        return report.exit_code()
