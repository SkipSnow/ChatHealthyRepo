"""What the baseline contains, from the disk.

Both promote and the commit driver need this answer, so it lives in one
place. Promote asks because a promote publishes the whole baseline. The
driver asks when a commit changes engineering_rules.json, because a rule
change alters what every file must satisfy -- and the 693 files that did
not change would otherwise never be tested against the rule that just
landed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

for _d in Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in sys.path:
            sys.path.insert(0, str(_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

_CH_LOG = ChatHealthyLoggingService()


def baseline_files(root: Path) -> list[str]:
    """Every file under the project root that git is not told to ignore.

    A promote publishes the whole baseline, so the whole baseline has to
    be answered for. `git diff --cached` cannot do that: a file identical
    to HEAD produces no diff entry, so it is carried into the commit
    having been examined by nothing. That is how a promote came to
    publish 697 files while the gate had looked at 7.

    So the list is built from the disk rather than from the index --
    walked, not asked for. git is consulted for one thing only, which is
    the one thing it is authoritative about: which paths the ignore rules
    exclude.

    Ignored DIRECTORIES are pruned rather than descended and discarded.
    Walking into node_modules, build and .venv to throw the results away
    cost 52,197 stat calls to keep 695 files.
    """
    found: list[str] = []
    pruned = 0

    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d != ".git"]

        if dirnames:
            candidates = [
                (here / d).relative_to(root).as_posix() for d in dirnames
            ]
            ignored_dirs = _ignored(candidates, root)
            keep = [
                d for d in dirnames
                if (here / d).relative_to(root).as_posix() not in ignored_dirs
            ]
            pruned += len(dirnames) - len(keep)
            dirnames[:] = keep

        for name in filenames:
            found.append((here / name).relative_to(root).as_posix())

    ignored_files = _ignored(found, root)
    kept = [f for f in found if f not in ignored_files]
    _CH_LOG.info(
        f"[baseline] walked {root.name}: {len(kept)} file(s) in the baseline "
        f"({len(found)} walked, {len(ignored_files)} ignored, "
        f"{pruned} ignored director(ies) pruned)"
    )
    return kept


def _ignored(paths: list[str], root: Path) -> set[str]:
    """Which of these paths the ignore rules exclude. One call.

    -z both ways. Newline-separated I/O cannot survive this: git quotes
    any path holding an unusual byte, and Windows line endings leave a
    stray CR on every path, so nothing an ignore rule matched compared
    equal to what was walked.
    """
    if not paths:
        return set()
    proc = subprocess.run(
        ["git", "check-ignore", "-z", "--stdin"],
        cwd=str(root), input=chr(0).join(paths),
        capture_output=True, text=True, check=False,
        # Git-for-Windows maps characters illegal in a Windows filename
        # into the private-use area -- a ':' becomes U+F03A -- so a path
        # can carry a character the console codepage cannot encode.
        encoding="utf-8", errors="surrogateescape",
    )
    if proc.returncode not in (0, 1):
        raise ChatHealthyException(
            mode="aborted",
            component="baseline_walk",
            message=f"git check-ignore failed (rc={proc.returncode}): "
            f"{proc.stderr.strip()[:300]}")
    return {p for p in proc.stdout.split(chr(0)) if p}
