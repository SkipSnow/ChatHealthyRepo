# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# bell_ringer.py — Workstation sound notifier for ChatHealthy.ai.
#
# Single class. Every caller in the codebase that needs to ring a bell
# uses this class.
#
# Backlog:
#   EPIC-008-F-004 (DevOps — Build, Deploy, and Environment Management)
#   Story EPIC-008-F-004-S-009 (Notify Human user with work station generated sound)

from __future__ import annotations


import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "FrontEndApplicationLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
_CH_LOG = ChatHealthyLoggingService()

log = ChatHealthyLoggingService()


class BellRinger:
    """Workstation sound notifier.

    Supports three ring patterns:
      - ring_once(): single beep
      - ring_n_times(n, ...): fixed count with configurable interval
      - ring_until_acknowledged(...): continuous beeps until stop() is called

    Optional integrations (configured via constructor or env):
      - MongoDB event log (admin.BellEvents)
      - Pushover cloud alert (PUSHOVER_TOKEN + PUSHOVER_USER)
    """

    def __init__(self, db_client=None, admin_db_name: str = "admin"):
        self.db_client = db_client
        self.admin_db_name = admin_db_name
        self._continuous_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    # ── Ring patterns ────────────────────────────────────────────

    def ring_once(self, frequency_hz: int = 880, duration_ms: int = 250) -> None:
        """Single beep. Synchronous; returns when the beep completes."""
        self._beep(frequency_hz, duration_ms)

    def ring_n_times(
        self,
        n: int,
        frequency_hz: int = 880,
        duration_ms: int = 250,
        interval_seconds: float = 0.15,
    ) -> None:
        """Ring n times with `interval_seconds` between beeps."""
        if n < 1:
            return
        for i in range(n):
            self._beep(frequency_hz, duration_ms)
            if i < n - 1 and interval_seconds > 0:
                time.sleep(interval_seconds)

    def ring_until_acknowledged(
        self,
        frequency_hz: int = 800,
        duration_ms: int = 600,
        interval_seconds: float = 5.0,
    ) -> None:
        """Start a daemon thread that beeps every `interval_seconds` until
        stop() is called. Idempotent: a no-op if a continuous ring is
        already in progress on this instance."""
        if self._continuous_thread and self._continuous_thread.is_alive():
            return
        self._stop_event.clear()
        self._continuous_thread = threading.Thread(
            target=self._continuous_loop,
            args=(frequency_hz, duration_ms, interval_seconds),
            daemon=True,
        )
        self._continuous_thread.start()
        log.info(
            "Continuous bell started — every %ss at %sHz/%sms",
            interval_seconds, frequency_hz, duration_ms,
        )

    def stop(self) -> None:
        """Stop the continuous-ring thread (if any)."""
        self._stop_event.set()
        log.info("Continuous bell stopped")

    # ── Event hook (used by fatal-error path and other event-driven callers) ──

    def log_event_and_ring(
        self,
        event_type: str,
        message: str,
        context: str = "",
        priority: str = "normal",
    ) -> None:
        """Log a bell event to admin.BellEvents (if db configured), ring the
        bell with a pattern matching `priority`, and emit Pushover alert
        for high/fatal priorities (if Pushover configured).

        priority:
          - "normal":  ring_once
          - "high":    ring_n_times(3)
          - "fatal":   ring_until_acknowledged (caller must call stop())
        """
        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "message": (message or "")[:2000],
            "context": context,
            "priority": priority,
            "hostname": os.environ.get(
                "COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")
            ),
        }

        if self.db_client is not None:
            try:
                self.db_client[self.admin_db_name]["BellEvents"].insert_one(event)
            except Exception as db_err:
                log.error("Failed to log bell event to MongoDB: %s", db_err)

        if priority == "fatal":
            self.ring_until_acknowledged()
        elif priority == "high":
            self.ring_n_times(3)
        else:
            self.ring_once()

        if priority in ("fatal", "high"):
            self._send_pushover(event_type, message, priority)

    # ── Internals ────────────────────────────────────────────────

    def _continuous_loop(self, freq: int, dur: int, interval: float) -> None:
        while not self._stop_event.is_set():
            self._beep(freq, dur)
            if self._stop_event.wait(interval):
                return

    def _beep(self, frequency_hz: int, duration_ms: int) -> None:
        """Platform-specific beep. Tries winsound first (in-process, no
        subprocess overhead), falls back to PowerShell, then to nothing."""
        # Skip beep entirely on Azure / HuggingFace where there's no audio.
        if os.environ.get("WEBSITE_SITE_NAME") or os.environ.get("SPACE_ID"):
            return
        try:
            import winsound  # noqa: WPS433 — Windows-only stdlib
            winsound.Beep(frequency_hz, duration_ms)
            return
        except (ImportError, RuntimeError):
            pass
        try:
            # CREATE_NO_WINDOW prevents PowerShell flashing a console window
            # when the bell rings (otherwise every beep pops a window).
            kwargs = {"timeout": 5, "capture_output": True}
            if sys.platform == "win32":
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            subprocess.run(
                ["powershell", "-Command",
                 f"[console]::beep({frequency_hz},{duration_ms})"],
                **kwargs,
            )
        except Exception:
            pass

    def _send_pushover(self, title: str, message: str, priority: str) -> None:
        token = os.environ.get("PUSHOVER_TOKEN")
        user = os.environ.get("PUSHOVER_USER")
        if not (token and user):
            return
        try:
            import requests  # noqa: WPS433 — only needed when Pushover is configured
            push_priority = 2 if priority == "fatal" else 0
            data = {
                "token": token,
                "user": user,
                "title": f"ChatHealthy.ai: {title}",
                "message": (message or "")[:500],
                "priority": push_priority,
            }
            if push_priority == 2:
                data.update({"retry": 30, "expire": 3600})
            requests.post(
                "https://api.pushover.net/1/messages.json",
                data=data, timeout=10,
            )
            log.info("Pushover alert sent (priority=%s)", priority)
        except Exception as push_err:
            log.error("Pushover send failed: %s", push_err)


def main() -> int:
    """CLI entry. Used by shell scripts and Claude Code via:
      python bell_ringer.py [once|twice|thrice|fatal]
    """
    pattern = sys.argv[1] if len(sys.argv) > 1 else "once"
    ringer = BellRinger()
    if pattern == "once":
        ringer.ring_once()
    elif pattern == "twice":
        ringer.ring_n_times(2)
    elif pattern == "thrice":
        ringer.ring_n_times(3)
    elif pattern == "fatal":
        ringer.ring_until_acknowledged()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ringer.stop()
    else:
        _CH_LOG.info(f"Unknown pattern: {pattern!r}. Use once|twice|thrice|fatal.",
              file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
