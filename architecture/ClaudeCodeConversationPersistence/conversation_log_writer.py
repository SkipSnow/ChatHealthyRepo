"""Conversation-log writer. Runs on every UserPromptSubmit and Stop hook.

Copies the payload from stdin to a spool file and exits. Per Rule-001 it does
not parse the payload and opens no network connection -- the hook sits on the
critical path of every interaction. Logging goes to a file through the
canonical logging service, which does neither.

    spool/write-<guid>-<time_ns>.txt     being written
    spool/read-<guid>-<time_ns>.txt      renamed in place, complete

The rename is what makes a file appear only once it is whole; a unique name
alone does not, because the name exists from the moment the file is created.
Rename is within one directory, so it is a metadata operation and atomic.

The time in the filename IS the timestamp of record -- the drain reads it from
there. A future change to this naming scheme silently destroys that field.

If the spool cannot be written the utterance is lost, and that is the only case
in the pipeline where anything is. It is made visible immediately.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

DEFAULT_SPOOL = Path(r"C:/tmp/ChatHealthySpool")

WRITE_PREFIX = "write-"
READ_PREFIX = "read-"
SUFFIX = ".txt"

FAILURE_PAGE_PATH = Path(tempfile.gettempdir()) / "conversation_log_spool_down.html"
FAILURE_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Conversation log spool is UNWRITABLE</title>
<style>
  html, body { margin: 0; padding: 0; height: 100vh; width: 100vw;
               overflow: hidden; background: #b00020; color: #fff;
               font-family: -apple-system, "Segoe UI", Arial, sans-serif; }
  .panel { position: fixed; inset: 0; display: flex;
           flex-direction: column; justify-content: center; align-items: center;
           text-align: center; padding: 2rem; box-sizing: border-box; }
  h1 { font-size: 4.5rem; margin: 0 0 1.5rem; }
  p  { font-size: 1.5rem; margin: 0.5rem 0; max-width: 64rem; line-height: 1.4; }
  code { background: rgba(0,0,0,0.25); padding: 0.1em 0.4em; border-radius: 0.25em; }
</style>
</head>
<body>
<div class="panel">
  <h1>Conversation log spool is UNWRITABLE</h1>
  <p>The spool directory <code>__SPOOL__</code> could not be written.</p>
  <p>__REASON__</p>
  <p>This utterance was NOT recorded. Check disk space and permissions.</p>
</div>
</body>
</html>"""


def spool_dir() -> Path:
    """The spool. Hard-coded -- one location, no environment to get wrong.

    Tests pass an explicit directory to write_payload() rather than redirecting
    this, so nothing needs to be configurable to keep them off the real spool.
    """
    return DEFAULT_SPOOL


def spool_name(guid: str, time_ns: int) -> str:
    """Zero-padded so the name is fixed width. Ordering is not relied on -- the
    archive is sorted in MongoDB -- but a ragged name makes the time field
    harder to parse and easy to get wrong."""
    return f"{guid}-{time_ns:020d}{SUFFIX}"


def write_payload(data: bytes, directory: Path) -> Path:
    """Write the payload and return the completed spool path. Raises on failure.

    The directory is a required argument and this function has no default: a
    fallback would be a branch that exists only for the tests.
    """
    directory.mkdir(parents=True, exist_ok=True)

    name = spool_name(uuid.uuid4().hex, time.time_ns())
    being_written = directory / (WRITE_PREFIX + name)
    complete = directory / (READ_PREFIX + name)

    with open(being_written, "wb") as handle:
        handle.write(data)
    os.replace(being_written, complete)
    return complete


def _show_failure_page(reason: str) -> None:
    html = (FAILURE_PAGE_HTML
            .replace("__SPOOL__", str(spool_dir()))
            .replace("__REASON__", reason))
    try:
        FAILURE_PAGE_PATH.write_text(html, encoding="utf-8")
    except OSError:
        return
    flags = subprocess.CREATE_NO_WINDOW | 0x00000008  # DETACHED_PROCESS
    subprocess.Popen(
        ["cmd", "/c", "start", "", str(FAILURE_PAGE_PATH)],
        creationflags=flags,
        close_fds=True,
    )


LOG_DIR = Path(__file__).resolve().parent / "log"

# The canonical logger, same as every other component. Rule-001 constrains this
# process to not parsing the payload and not opening a network connection; a
# file destination does neither. The folder is resolved from this file's own
# location so the deployment's three executables share one log folder without
# anything passing a path.
os.environ.setdefault("CH_LOG_DESTINATION", str(LOG_DIR))
os.environ.setdefault("CH_COMPONENT", "conversation_log_writer")
os.environ.setdefault("CH_SPACE_NAME", "conversation-log-writer")

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "FrontEndApplicationLib" / "src"))
from chathealthy_frontend_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log_service = ChatHealthyLoggingService()


def _log(message: str) -> None:
    """Never raises. A logging failure must not cost the utterance."""
    try:
        _log_service.info("writer: %s", message)
    except Exception:
        pass


def _log_lost(message: str, exc: Exception) -> None:
    """The one place an utterance is lost. Always recorded."""
    try:
        _log_service.error(
            "writer: %s", message,
            exc=ChatHealthyException(
                mode="spool_write_failed",
                component="ConversationLogWriter",
                message=message,
                exception=exc,
            ),
        )
    except Exception:
        pass


def main() -> int:
    data = sys.stdin.buffer.read()
    try:
        path = write_payload(data, spool_dir())
    except OSError as exc:
        # The one place in the pipeline where an utterance is lost.
        _log_lost(f"SPOOL WRITE FAILED - UTTERANCE LOST: {exc}", exc)
        _show_failure_page(f"{type(exc).__name__}: {exc}")
        return 1
    _log(f"spooled {path.name} ({len(data)} bytes)")
    _alarm_if_backlogged()
    return 0


def _alarm_if_backlogged() -> None:
    """Tell the operator if the spool has stopped draining.

    This lives in the writer, not the supervisor, for one reason: the writer
    runs as a Claude Code hook in the operator's own session, and the
    supervisor runs as a Windows service in Session 0, which cannot put
    anything on the interactive desktop. The supervisor's alarm opened
    browsers nobody could see.

    Runs AFTER the utterance is safely spooled, and swallows everything: an
    alarm that fails must never cost the utterance it was warning about.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from conversation_log_alarm import check_and_alarm
        check_and_alarm(spool_dir())
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
