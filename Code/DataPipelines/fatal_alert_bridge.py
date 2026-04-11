# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Pipeline Architecture v4 — FatalAlertBridge
# v4-008: Ring bell on fatal pipeline errors.

import logging
import os
import subprocess
import threading
import time
from datetime import datetime, timezone

log = logging.getLogger("fatal_alert")

_bell_thread = None
_bell_stop = threading.Event()


def _bell_loop():
    """Ring PowerShell bell every 5 seconds until stopped."""
    while not _bell_stop.is_set():
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "[console]::beep(800,600); Start-Sleep -Milliseconds 200; "
                 "[console]::beep(800,600); Start-Sleep -Milliseconds 200; "
                 "[console]::beep(800,600)"],
                timeout=10, capture_output=True,
            )
        except Exception:
            print("\a\a\a", flush=True)
        _bell_stop.wait(5)


class FatalAlertBridge:
    """Fatal error alerting per v4-008.

    Local: PowerShell beep loop every 5 seconds.
    Cloud: Pushover webhook (if PUSHOVER_TOKEN set).
    Events logged to admin.BellEvents.
    """

    def __init__(self, db_client=None, admin_db_name: str = "admin"):
        self.db_client = db_client
        self.admin_db_name = admin_db_name

    def send_alert(self, error: Exception, context: str = ""):
        """Fire fatal alert. Starts bell loop and logs event."""
        global _bell_thread

        event = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "error_type": type(error).__name__,
            "error_message": str(error)[:2000],
            "context": context,
            "hostname": os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown")),
        }

        log.critical("FATAL ALERT: %s — %s", context, error)

        # Log to MongoDB if available
        if self.db_client:
            try:
                self.db_client[self.admin_db_name]["BellEvents"].insert_one(event)
            except Exception as db_err:
                log.error("Failed to log bell event to MongoDB: %s", db_err)

        # Start bell loop (local)
        if not os.environ.get("WEBSITE_SITE_NAME"):
            if _bell_thread is None or not _bell_thread.is_alive():
                _bell_stop.clear()
                _bell_thread = threading.Thread(target=_bell_loop, daemon=True)
                _bell_thread.start()
                log.info("Bell loop started — will ring every 5 seconds")

        # Pushover webhook (cloud)
        pushover_token = os.environ.get("PUSHOVER_TOKEN")
        pushover_user = os.environ.get("PUSHOVER_USER")
        if pushover_token and pushover_user:
            try:
                import requests
                requests.post("https://api.pushover.net/1/messages.json", data={
                    "token": pushover_token,
                    "user": pushover_user,
                    "title": f"PIPELINE FATAL: {context}",
                    "message": str(error)[:500],
                    "priority": 2,
                    "retry": 30,
                    "expire": 3600,
                }, timeout=10)
                log.info("Pushover alert sent")
            except Exception as push_err:
                log.error("Pushover alert failed: %s", push_err)

    @staticmethod
    def stop_bell():
        """Stop the bell loop."""
        _bell_stop.set()
        log.info("Bell loop stopped")
