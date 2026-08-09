"""Tell the operator the conversation log has stopped moving.

Why this is Python and not the C# supervisor: the supervisor runs as a Windows
service under LocalSystem, in Session 0. A Session 0 process cannot render on
the interactive desktop, so every browser it opened was invisible -- the alarm
appeared to work and reached nobody. The writer runs as a Claude Code hook, in
the operator's own session, so it is the only component here that can put
something on the screen.

The check is a directory listing. No parse, no network, no database.
"""

from __future__ import annotations

import os
import tempfile
import time
import webbrowser
from pathlib import Path

# Past this the spool is not draining. Same value the supervisor used, now in
# one place instead of two.
ALARM_AGE_SECONDS = 1800

# Anything still spooled beyond this is deleted, so this is the deadline the
# operator is actually racing.
PURGE_AGE_HOURS = 24

# One alarm per cooldown, or every utterance would open a browser tab.
COOLDOWN_SECONDS = 900

_ALARM_PAGE = "conversation_log_not_draining.html"
_COOLDOWN_MARKER = "conversation_log_alarm_last.txt"
# Written by the drain, read by the writer. The drain is the only component
# that talks to MongoDB, so it is the only one that can say a write failed --
# and it runs in Session 0, so it is the one component that cannot say it to
# the operator. Hence a file: the knower records, the visible process shows.
_FAILURE_MARKER = "conversation_log_drain_failure.txt"


def _temp(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name


def oldest_ready_age_seconds(directory: Path, now_ns: int | None = None) -> float:
    """Age of the oldest unshipped utterance, from the filename the writer
    stamped. Costs one directory listing and opens no file."""
    if not directory.is_dir():
        return 0.0
    now_ns = time.time_ns() if now_ns is None else now_ns
    oldest = 0.0
    for path in directory.iterdir():
        name = path.name
        if not name.startswith("read-") or not name.endswith(".txt"):
            continue
        stem = name[len("read-"):-len(".txt")]
        _guid, _, time_part = stem.rpartition("-")
        if not time_part.isdigit():
            continue
        age = (now_ns - int(time_part)) / 1e9
        if age > oldest:
            oldest = age
    return oldest


def record_drain_failure(reason: str) -> None:
    """Called by the drain when a shipment fails. Records WHY, from the only
    component that knows. Never raises -- the drain is already failing."""
    try:
        _temp(_FAILURE_MARKER).write_text(
            "{}\n{}".format(time.time(), reason), encoding="utf-8")
    except OSError:
        pass


def clear_drain_failure() -> None:
    """Called by the drain after a successful shipment. The failure is over,
    so the operator should not be told it is still happening."""
    try:
        _temp(_FAILURE_MARKER).unlink()
    except OSError:
        pass


def pending_drain_failure() -> str | None:
    """The reason the drain last recorded, or None."""
    try:
        raw = _temp(_FAILURE_MARKER).read_text(encoding="utf-8")
    except OSError:
        return None
    _stamp, _, reason = raw.partition("\n")
    return reason.strip() or "unspecified"


def _in_cooldown(now: float) -> bool:
    marker = _temp(_COOLDOWN_MARKER)
    try:
        return (now - float(marker.read_text(encoding="utf-8").strip())) < COOLDOWN_SECONDS
    except (OSError, ValueError):
        return False


def _page_html(age_seconds: float, stopped: bool, spool_dir: Path,
               reason: str | None = None) -> str:
    headline = ("Conversation log has STOPPED" if stopped
                else "Conversation log is NOT draining")
    detail = ("The drain crash-looped and the service gave up. It will NOT "
              "retry until restarted."
              if stopped else
              "The drain is running but is not shipping.")
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{headline}</title>
<style>
  html,body{{margin:0;height:100vh;width:100vw;overflow:hidden;
    background:#b00020;color:#fff;
    font-family:-apple-system,"Segoe UI",Arial,sans-serif}}
  .p{{position:fixed;inset:0;display:flex;flex-direction:column;
    justify-content:center;align-items:center;text-align:center;padding:2rem}}
  h1{{font-size:4.5rem;margin:0 0 1.5rem}}
  p{{font-size:1.5rem;margin:.5rem 0;max-width:64rem;line-height:1.4}}
</style></head><body><div class="p">
<h1>{headline}</h1>
<p>{detail}</p>
<p>The oldest unshipped utterance is {int(age_seconds)} seconds old.</p>
{f"<p>Reported by the drain: {reason}</p>" if reason else ""}
<p>Utterances are safe on disk at {spool_dir} &mdash; nothing is lost yet.</p>
<p>Anything older than {PURGE_AGE_HOURS} hours is DELETED at midnight.</p>
</div></body></html>"""


def raise_alarm(age_seconds: float, spool_dir: Path, stopped: bool = False,
                reason: str | None = None) -> bool:
    """Put the failure on the operator's screen. True if a page was opened.

    Never raises: this runs inside the hook that captures the utterance, and
    an alarm that fails must not also cost the utterance.
    """
    now = time.time()
    if _in_cooldown(now):
        return False
    page = _temp(_ALARM_PAGE)
    try:
        page.write_text(_page_html(age_seconds, stopped, spool_dir, reason),
                        encoding="utf-8")
        webbrowser.open(page.as_uri())
        _temp(_COOLDOWN_MARKER).write_text(str(now), encoding="utf-8")
        return True
    except Exception:
        return False


def check_and_alarm(spool_dir: Path) -> bool:
    """Called by the writer once the utterance is safely spooled. Alarms only
    when the backlog is older than the threshold."""
    if os.environ.get("CH_LOG_ALARM_DISABLED", "").strip() == "1":
        return False
    age = oldest_ready_age_seconds(spool_dir)

    # The drain SAYING it failed beats this process GUESSING from file ages.
    # A recorded reason alarms immediately -- waiting out a 30 minute age
    # threshold to report a failure already known is just a delayed alarm.
    reason = pending_drain_failure()
    if reason:
        return raise_alarm(age, spool_dir, stopped=False, reason=reason)

    # No reason recorded means nobody wrote one: the drain is not running at
    # all. Age is the only evidence left, so it is the fallback, not the test.
    if age < ALARM_AGE_SECONDS:
        return False
    return raise_alarm(age, spool_dir, stopped=True)
