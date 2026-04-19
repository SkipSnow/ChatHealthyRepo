# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Close BUG-LANGCHAIN-CONTAINER-* bugs whose handlers are now LangGraph-
# wrapped. Per Human directive 2026-04-19. Mechanical handlers in main.py
# stay open (still exempted, deferred work).

from __future__ import annotations

import json
from pathlib import Path
from datetime import date

REPO = Path(r"c:/chatHealthy/findCare")
BUGS = REPO / "brain/machine_artifacts/content/bugs.json"
TODAY = date.today().isoformat()

# Containers that are now graph-routed (close their bugs)
WRAPPED = {
    # shared_services — all 5 wrapped (mechanical, but real graphs now)
    "BUG-LANGCHAIN-CONTAINER-APP-HEALTH",
    "BUG-LANGCHAIN-CONTAINER-APP-SPLASH",
    "BUG-LANGCHAIN-CONTAINER-APP-TRANSFER-TO-FINDCARE",
    "BUG-LANGCHAIN-CONTAINER-APP-VERIFY-TOKEN",
    "BUG-LANGCHAIN-CONTAINER-APP-GET-SECRET",
    # evaluate_care — all 11 wrapped (6 mechanical + 5 business-logic)
    "BUG-LANGCHAIN-CONTAINER-APP-DEBUG-VERIFY-LIVE",
    "BUG-LANGCHAIN-CONTAINER-APP-DEBUG-BOOTSTRAP",
    "BUG-LANGCHAIN-CONTAINER-APP-SCORE-PROVIDER",
    "BUG-LANGCHAIN-CONTAINER-APP-SCORE-TRIAL",
    "BUG-LANGCHAIN-CONTAINER-APP-EXPLAIN",
    "BUG-LANGCHAIN-CONTAINER-APP-EVALUATE-PROVIDERS",
    "BUG-LANGCHAIN-CONTAINER-APP-EVALUATE-VIEW",
    # main.py — only the 5 business-logic ones wrapped
    "BUG-LANGCHAIN-CONTAINER-MAIN-SEARCH",
    "BUG-LANGCHAIN-CONTAINER-MAIN-CLASSIFY",
    "BUG-LANGCHAIN-CONTAINER-MAIN-QA-REPORT",
    "BUG-LANGCHAIN-CONTAINER-MAIN-QA-REPORT-SUBMIT",
    "BUG-LANGCHAIN-CONTAINER-MAIN-WELCOME",
}

# main.py mechanical handlers — STAY OPEN (still exempted, deferred):
# - BUG-LANGCHAIN-CONTAINER-MAIN-HEALTH
# - BUG-LANGCHAIN-CONTAINER-MAIN-GET-SESSION
# - BUG-LANGCHAIN-CONTAINER-MAIN-EVALUATE-SPLASH
# - BUG-LANGCHAIN-CONTAINER-MAIN-TRANSFER-TO-FINDCARE
# - BUG-LANGCHAIN-CONTAINER-MAIN-EVALUATE-PROXY
# - BUG-LANGCHAIN-CONTAINER-MAIN-SHARED-SPLASH
# - BUG-LANGCHAIN-CONTAINER-MAIN-SHARED-VERIFY-TOKEN
# - BUG-LANGCHAIN-CONTAINER-MAIN-EVALUATE-VERIFY-TOKEN
# - BUG-LANGCHAIN-CONTAINER-MAIN-SERVE-INDEX


def main():
    with open(BUGS, encoding="utf-8") as f:
        bg = json.load(f)
    closed = 0
    for b in bg.get("bug", []):
        bid = b.get("bugid", "")
        if bid in WRAPPED:
            if b.get("status") == "tested_passed":
                continue
            b["status"] = "tested_passed"
            existing_fix = b.get("fix", "")
            close_note = (
                f"Closed {TODAY}: handler refactored to invoke through "
                f"LangGraph StateGraph. v4-043 scanner reports zero "
                f"violations. Smoke test (Code/Shared/ops/tools/tests/"
                f"test_langgraph_compliance_smoke.py) verifies. UAT in "
                f"Studio still pending per BRAIN-DEFERRED-WORK pattern "
                f"(human signs off via LangSmith annotation queue once "
                f"deployed)."
            )
            b["fix"] = (existing_fix + " | " + close_note) if existing_fix else close_note
            closed += 1
    bg["bug"] = bg["bug"]
    with open(BUGS, "w", encoding="utf-8") as f:
        json.dump(bg, f, indent=2)
    print(f"Closed {closed} wrapped-container bugs (status -> tested_passed)")
    print(f"Left open: 9 mechanical handlers in main.py "
          f"(still # graph-exempt, deferred work)")


if __name__ == "__main__":
    main()
