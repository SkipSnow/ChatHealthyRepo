#!/bin/bash
# Daily conversation log commit — runs via Windows Task Scheduler at 11:55 PM PST
# Only commits if conversation_log.json has changed. Zero noise on quiet days.
# Copyright (c) 2026 Skip Snow. All rights reserved.

# BUG-002: resolve project root via env var (set at machine login by the
# CHATHEALTHY_PROJECT_ROOT system env var) so the daily commit task works
# regardless of where the repo is cloned.
if [ -z "$CHATHEALTHY_PROJECT_ROOT" ]; then
    echo "ERROR: CHATHEALTHY_PROJECT_ROOT env var not set" >&2
    exit 1
fi
cd "$CHATHEALTHY_PROJECT_ROOT" || exit 1

LOG="brain/machine_artifacts/content/conversation_log.json"

# Exit if file unchanged
git diff --quiet "$LOG" && exit 0

# Commit and push
git add "$LOG"
git commit -m "[Unattended Mode] Daily conversation log"
git push
