"""Delete spooled utterances that will never be archived.

Captured content that has not reached the archive within the retention window
is deleted, and the loss is recorded so it is known to have happened
(EPIC-008-F-013-S-002). That is a business rule about the operator's own words,
so it lives here rather than in the supervisor, which only decides WHEN to run
it, never WHAT it does.

Run as a script:  python conversation_log_sweep.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

from conversation_log_writer import READ_PREFIX, SUFFIX, WRITE_PREFIX, spool_dir

# Beyond this a spooled utterance is deleted, whatever the reason it is still
# there. An outage shorter than this loses nothing; longer loses the remainder.
PURGE_AGE_HOURS = 24


def _time_ns_from_name(name: str) -> int | None:
    """read-|write-<guid>-<time_ns>.txt -> time_ns, or None."""
    if not name.endswith(SUFFIX):
        return None
    if not (name.startswith(READ_PREFIX) or name.startswith(WRITE_PREFIX)):
        return None
    prefix = READ_PREFIX if name.startswith(READ_PREFIX) else WRITE_PREFIX
    stem = name[len(prefix):-len(SUFFIX)]
    _guid, _, time_part = stem.rpartition("-")
    return int(time_part) if time_part.isdigit() else None


def purge(directory: Path, age_hours: int = PURGE_AGE_HOURS,
          now_ns: int | None = None) -> list[str]:
    """Delete every spool file older than the window. Returns what was lost.

    The names are returned rather than a count because each one is an
    utterance nobody will ever read again, and the record of the loss is the
    only trace left of it.
    """
    if not directory.is_dir():
        return []
    now_ns = time.time_ns() if now_ns is None else now_ns
    cutoff = now_ns - age_hours * 3600 * 1_000_000_000
    lost: list[str] = []
    for path in directory.iterdir():
        stamp = _time_ns_from_name(path.name)
        if stamp is None or stamp >= cutoff:
            continue
        try:
            path.unlink()
            lost.append(path.name)
        except OSError:
            continue
    return lost


def main() -> int:
    lost = purge(spool_dir())
    if lost:
        # stderr, because the supervisor captures it into the event log. The
        # loss must be visible; it is the one place utterances are destroyed.
        sys.stderr.write(
            f"sweep: deleted {len(lost)} spooled utterance(s) older than "
            f"{PURGE_AGE_HOURS}h. Those utterances are gone.\n"
        )
        for name in lost:
            sys.stderr.write(f"sweep: lost {name}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
