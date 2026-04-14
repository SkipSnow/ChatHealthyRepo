# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# BRAIN-CONVERSATION-LOG-REQ-B004 / REQ-T001:
# Windows service that purges conversation_log.json to the last 24 hours.
# Runs daily at 2:00 AM PST. Independent of Claude Code and Cursor.
#
# Install:
#   python conversation_log_purge_service.py install
#   python conversation_log_purge_service.py start
#
# Uninstall:
#   python conversation_log_purge_service.py stop
#   python conversation_log_purge_service.py remove
#
# Manual run (no service, just purge now):
#   python conversation_log_purge_service.py --purge-now

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
LOG_PATH = REPO_ROOT / "brain" / "machine_artifacts" / "content" / "conversation_log.json"
SERVICE_LOG = REPO_ROOT / "test_output" / "conversation_log_purge.log"

PURGE_HOUR_PST = 2  # 2:00 AM PST
RETENTION_HOURS = 24

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(SERVICE_LOG, encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
_log = logging.getLogger("purge_service")


def purge_old_entries():
    """Remove conversation log entries older than 24 hours."""
    if not LOG_PATH.exists():
        _log.info("No conversation_log.json found at %s", LOG_PATH)
        return 0

    try:
        with open(LOG_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        _log.error("Failed to read conversation_log.json: %s", e)
        return 0

    utterances = data.get("utterances", [])
    if not utterances:
        _log.info("No utterances to purge")
        return 0

    cutoff = datetime.now(timezone.utc) - timedelta(hours=RETENTION_HOURS)
    original_count = len(utterances)

    kept = []
    for u in utterances:
        ts = u.get("timestamp_utc", "")
        if not ts:
            kept.append(u)  # Keep entries without timestamps
            continue
        try:
            entry_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if entry_time >= cutoff:
                kept.append(u)
        except (ValueError, TypeError):
            kept.append(u)  # Keep entries with unparseable timestamps

    purged = original_count - len(kept)
    if purged > 0:
        data["utterances"] = kept
        tmp = LOG_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        tmp.replace(LOG_PATH)
        _log.info("Purged %d entries older than %d hours. %d remaining.", purged, RETENTION_HOURS, len(kept))
    else:
        _log.info("No entries older than %d hours. %d entries retained.", RETENTION_HOURS, len(kept))

    return purged


def next_purge_time():
    """Calculate seconds until next 2:00 AM PST."""
    # PST = UTC-8, PDT = UTC-7. Use -7 for PDT (current).
    pst_offset = timedelta(hours=-7)
    now_utc = datetime.now(timezone.utc)
    now_pst = now_utc + pst_offset

    target = now_pst.replace(hour=PURGE_HOUR_PST, minute=0, second=0, microsecond=0)
    if now_pst >= target:
        target += timedelta(days=1)

    delta = target - now_pst
    return delta.total_seconds()


def run_loop():
    """Main service loop. Sleeps until 2 AM PST, purges, repeats."""
    _log.info("Conversation log purge service started. Retention: %d hours. Purge at %d:00 AM PST.",
              RETENTION_HOURS, PURGE_HOUR_PST)

    while True:
        wait = next_purge_time()
        _log.info("Next purge in %.1f hours (%.0f seconds)", wait / 3600, wait)
        time.sleep(wait)

        _log.info("Purge triggered at 2:00 AM PST")
        try:
            purge_old_entries()
        except Exception as e:
            _log.error("Purge failed: %s", e)

        # Sleep a minute to avoid double-firing
        time.sleep(60)


# Windows service support via pywin32
try:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager

    class ConversationLogPurgeService(win32serviceutil.ServiceFramework):
        _svc_name_ = "ChatHealthyLogPurge"
        _svc_display_name_ = "ChatHealthy Conversation Log Purge"
        _svc_description_ = "Purges conversation_log.json to the last 24 hours daily at 2:00 AM PST."

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self.stop_event = win32event.CreateEvent(None, 0, 0, None)
            self.running = True

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self.running = False
            win32event.SetEvent(self.stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                                 servicemanager.PYS_SERVICE_STARTED,
                                 (self._svc_name_, ''))
            _log.info("Windows service started")

            while self.running:
                wait = min(next_purge_time(), 3600)  # Check every hour at most
                result = win32event.WaitForSingleObject(self.stop_event, int(wait * 1000))

                if result == win32event.WAIT_OBJECT_0:
                    break

                # Check if it's purge time
                pst_offset = timedelta(hours=-7)
                now_pst = datetime.now(timezone.utc) + pst_offset
                if now_pst.hour == PURGE_HOUR_PST and now_pst.minute < 5:
                    try:
                        purge_old_entries()
                    except Exception as e:
                        _log.error("Purge failed: %s", e)
                    # Sleep past the purge window
                    time.sleep(360)

            _log.info("Windows service stopped")

    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


if __name__ == "__main__":
    if "--purge-now" in sys.argv:
        purge_old_entries()
    elif HAS_WIN32 and len(sys.argv) > 1 and sys.argv[1] in ("install", "start", "stop", "remove", "restart"):
        win32serviceutil.HandleCommandLine(ConversationLogPurgeService)
    else:
        # Run as standalone loop (no Windows service)
        run_loop()
