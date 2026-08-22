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

It records and it emails, and those are different in kind from a rule
violation. A violation is ordinary: the scans catch dozens in a working day
and the fix is to correct the code, so a message per occurrence would train
the operator to ignore the channel. Going around the gate is not ordinary.
It should happen never, and the operator should hear about it at the moment
it happens rather than whenever someone next reads a collection.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timezone
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

# The credentials that reach Atlas, and the address to notify, live in .env
# at the repository root, and this runs from a git hook that inherits none
# of them. Existing values win,
# so nothing here overrides a deliberate setting.
for _p in _HERE.parents:
    if (_p / ".git").exists():
        _env = _p / ".env"
        if _env.is_file():
            from dotenv import dotenv_values
            for _k, _v in dotenv_values(_env).items():
                if _v is not None and _k not in os.environ:
                    os.environ[_k] = _v
        break

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
        # When the bypass was attempted, which is not the commit's author
        # date: an amended or replayed commit carries an older one, and the
        # question this answers is when someone went around the gate.
        "attempted_at": datetime.now(timezone.utc).isoformat(),
    }


def _email(document: dict) -> None:
    """Tell the operator, at the moment it happens.

    A record in a collection is evidence for whoever goes looking, and nobody
    goes looking at the moment it happens -- which is when the operator can
    still ask why. So this one is pushed rather than filed: every file the
    commit tried to carry, the assertion that all of them were rolled back,
    and when the bypass was attempted.

    Failure to send never blocks the rollback, and is itself reported.
    """
    from chathealthy_lib.notification_client import NotificationClient

    to = os.environ.get("NOTIFICATION_TO_EMAIL", "").strip()
    if not to:
        _log.error("[circumvention] NOTIFICATION_TO_EMAIL is not set, so the "
                   "attempt was recorded and nobody was told")
        return

    when = document["attempted_at"]
    files = document["files"]
    listed = chr(10).join(f"  - {f}" for f in files) or "  (none reported)"

    body = (
        f"A commit was made around the Rule-065 approval gate and has been "
        f"rolled back.{chr(10)}{chr(10)}"
        f"Attempted at   {when}{chr(10)}"
        f"Reason         {document['reason']}{chr(10)}"
        f"Branch         {document['branch']}{chr(10)}"
        f"Author         {document['author']}{chr(10)}"
        f"Commit         {document['commit']}{chr(10)}"
        f"Message        {document['commit_subject']}{chr(10)}{chr(10)}"
        f"Files it tried to commit ({len(files)}):{chr(10)}{listed}{chr(10)}{chr(10)}"
        f"Every one of these files was rolled back. The commit object was "
        f"unmade and the branch points where it did before. The changes "
        f"remain staged in the working tree, so nothing was lost and nothing "
        f"was written to the repository.{chr(10)}{chr(10)}"
        f"This is not sent for a rule violation, which is ordinary. It is "
        f"sent only when the approval gate was gone around, which should "
        f"happen never.{chr(10)}")

    try:
        NotificationClient().send_email(
            to=to,
            subject=(f"Rule-065 gate bypassed and rolled back -- "
                     f"{len(files)} file(s), {when}"),
            body=body,
            log_context="circumvention_attempt")
    except Exception as exc:  # noqa: BLE001 - the rollback proceeds regardless
        _log.error(f"[circumvention] the operator could not be emailed: {exc}")


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
    _email(document)
    return 0


if __name__ == "__main__":
    sys.exit(main())
