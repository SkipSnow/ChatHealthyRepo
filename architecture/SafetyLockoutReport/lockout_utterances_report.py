# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
"""Monthly operational report — every lockout utterance, ordered by frequency.

The safety lockout is crowd-sourced: every lockout grows
`Users.UserLockOutUtterances` (one record per distinct UPPERCASED utterance,
with a count of how many lockouts it has caused). This report reads that
collection and writes a spreadsheet for ChatHealthy staff to curate —
which learned utterances become trusted deterministic triggers.

For now the utterances are not categorized: the report is just the
utterance and its count, ordered by frequency (most-locked-out first).

Run locally:  python architecture/SafetyLockoutReport/lockout_utterances_report.py --identity claudeCodeAgent --out <path>
As a runbook: run_as_runbook() reads the collection under the runbook's identity.

The report LOGIC (build_workbook) is separate from the connection so it can
be tested against records without a live cluster.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException

log = ChatHealthyLoggingService()

_DB = "Users"
_COLLECTION = "UserLockOutUtterances"
_CLUSTER = "ChatHealthyFrontEnd"
_COLUMNS = ("record_number", "utterance", "count")


def build_workbook(records: list[dict[str, Any]]):
    """Records -> an .xlsx workbook, ordered by frequency (count desc), then
    by record_number so ties are stable. One header row, one row per record.
    """
    import openpyxl

    ordered = sorted(
        records,
        key=lambda r: (-int(r.get("count", 0) or 0), int(r.get("record_number", 0) or 0)),
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Lockout Utterances"
    ws.append(list(_COLUMNS))
    for r in ordered:
        ws.append([r.get("record_number"), r.get("utterance"), r.get("count")])
    return wb


def read_utterances(identity: str, cluster: str = _CLUSTER) -> list[dict[str, Any]]:
    """Every lockout-utterance record, read under the given identity."""
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities

    coll = ChatHealthyMongoUtilities().getConnection(identity, cluster)[_DB][_COLLECTION]
    return list(coll.find({}, {"_id": 0, "record_number": 1, "utterance": 1, "count": 1}))


def write_report(identity: str, out_path: Path) -> Path:
    records = read_utterances(identity)
    wb = build_workbook(records)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(out_path))
    log.info("lockout-utterances report: %d utterance(s) -> %s", len(records), out_path)
    return out_path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Monthly lockout-utterances operational report.")
    ap.add_argument("--identity", required=True,
                    help="The identity that reads Users.UserLockOutUtterances.")
    ap.add_argument("--out", default="lockout_utterances_report.xlsx",
                    help="Where to write the .xlsx.")
    args = ap.parse_args(argv)
    write_report(args.identity, Path(args.out))
    return 0


def run_as_runbook() -> int:
    """Automation-account entry point: read under the runbook's own identity
    and write the report beside the runbook for the delivery step to pick up.
    """
    import os
    identity = os.environ.get("REPORT_IDENTITY", "")
    if not identity:
        raise ChatHealthyException(
            mode="config_error",
            component="lockout_utterances_report",
            message="REPORT_IDENTITY not set; the runbook needs the identity that reads Users.",
        )
    write_report(identity, Path("lockout_utterances_report.xlsx"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
