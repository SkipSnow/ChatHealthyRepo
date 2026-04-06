#!/bin/bash
# Daily conversation log commit — runs via Windows Task Scheduler at 11:55 PM PST
# Only commits if conversation_log.json has changed. Zero noise on quiet days.
# Copyright (c) 2026 Skip Snow. All rights reserved.

cd "c:/chatHealthy/findCare" || exit 1

LOG="brain/machine_artifacts/content/conversation_log.json"

# Exit if file unchanged
git diff --quiet "$LOG" && exit 0

# Commit and push
git add "$LOG"
git commit -m "[Unattended Mode] Daily conversation log"
git push
