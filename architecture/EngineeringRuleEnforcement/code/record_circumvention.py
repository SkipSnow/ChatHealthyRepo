"""Record an attempt to commit around the approval gate.

post-commit unmakes a commit that carries no evidence of approval. Unmaking
it silently would leave the attempt invisible: the tree looks untouched, the
operator sees a terminal message that scrolls away, and nothing durable says
it happened. An agent that can try repeatedly without leaving a trace is an
agent nobody can hold to anything.

So the attempt is written where the repository cannot reach it, beside the
approvals it went around and told apart by authorization_type. The commit is
already unreferenced by the time anyone reads this record, which is why the
record carries the message, the author and the files rather than a hash
someone would have to go and resolve.

Recording never blocks the rollback. Called after the facts are captured and
before the ref moves, it fails loudly and returns, because an attempt that
could not be recorded still has to be undone.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / ".git").exists():
        _lib = _parent / "ChatHealthyLib" / "src"
        if str(_lib) not in sys.path:
            sys.path.insert(0, str(_lib))
        break
if str(_HERE.parent) not in sys.path:
    sys.path.insert(0, str(_HERE.parent))

os.environ.setdefault("CH_LOG_DESTINATION", "stderr")

from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

import authorization_record  # noqa: E402

_log = ChatHealthyLoggingService()

AUTHORIZATION_TYPE = "circumvention_attempt"


def _git(*args: str) -> str:
    proc = subprocess.run(["git", *args], capture_output=True, text=True,
                          check=False)
    return proc.stdout.strip() if proc.returncode == 0 else ""


def facts(reason: str) -> dict:
    """What the attempt was, gathered while the commit is still referenced."""
    return {
        "authorization_type": AUTHORIZATION_TYPE,
        "reason": reason,
        "commit": _git("rev-parse", "HEAD"),
        "commit_subject": _git("log", "-1", "--pretty=%s"),
        "commit_message": _git("log", "-1", "--pretty=%B"),
        "author": _git("log", "-1", "--pretty=%an <%ae>"),
        "committed_at": _git("log", "-1", "--pretty=%aI"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "files": [f for f in _git("show", "--name-only", "--pretty=format:",
                                  "HEAD").splitlines() if f.strip()],
        "operator": os.environ.get("USERNAME") or os.environ.get("USER")
                    or "unknown",
        "rolled_back": True,
    }


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    reason = args[0] if args else "no evidence of approval"

    document = facts(reason)
    _log.error(
        f"[circumvention] a commit was made around the approval gate and is "
        f"being unmade: {document['commit'][:12]} "
        f"{document['commit_subject']!r} by {document['author']} "
        f"({len(document['files'])} file(s))")
    try:
        authorization_record.append(document, tolerate_failure=True)
    except Exception as exc:  # noqa: BLE001 - the rollback proceeds regardless
        _log.error(f"[circumvention] the attempt could not be recorded: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
