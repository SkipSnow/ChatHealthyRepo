# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Watch a Medicare run and record what it and its machines are doing.

An operator tool, not a step: it reads and writes nothing the run depends on.
It samples every 60 seconds and writes two things.

  * a spreadsheet, one row per sample, carrying the system facts -- per VM CPU
    average and maximum, available memory, network in, OS-disk IOPS percent --
    beside the run's own progress, so the two can be read against each other.
  * a text log of the same samples, for reading while the run is live.

Both paths are arguments with defaults, because an operator watching a run
should not have to remember where this writes.

A sample that cannot be taken is recorded as the reason and the loop
continues. A monitor that dies on a transient is worse than no monitor, and
the run it is watching lasts a day.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import pathlib
import subprocess
import sys
import time

# The shared library is not pip-installed in the workstation venv; every
# repo program reaches it by path, and an operator tool run from a shell
# gets no help from a container's PYTHONPATH.
_REPO = pathlib.Path(__file__).resolve().parents[3]
for _p in (str(_REPO), str(_REPO / 'ChatHealthyLib' / 'src')):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# A container is handed its connection facts as environment variables; an
# operator shell is not, so the workstation's .env is loaded first. The
# Mongo utility needs KEY_VAULT_URI to read a connection fact at all.
from dotenv import load_dotenv  # noqa: E402
load_dotenv(_REPO / '.env')

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService)
from chathealthy_lib.mongo_utilities import (  # noqa: E402
    ChatHealthyMongoUtilities)

_log = ChatHealthyLoggingService()

RESOURCE_GROUP = "rg-chathealthy-pipeline-dev"
AUTOMATION_ACCOUNT = "ChatHealthyJobManager"
PIPELINE_NAME = "medicare"
TERMINAL = ("success", "failed", "aborted")

METRICS = (
    "Percentage CPU",
    "Available Memory Bytes",
    "Network In Total",
    "OS Disk IOPS Consumed Percentage",
)

COLUMNS = (
    "sampled_at_utc", "job_name", "job_status",
    "run_id", "run_status", "steps_done", "last_step", "last_step_status",
    "controller_heartbeat_at",
    "work_pending", "work_running", "work_done", "work_failed",
    "findings_total",
    "vm_name", "vm_size", "vm_provisioning",
    "cpu_avg_pct", "cpu_max_pct",
    "available_memory_bytes", "network_in_total", "os_disk_iops_pct",
)


def _utc_now_text() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _az(args: list[str]) -> str:
    """Azure CLI stdout, or an empty string. Never raises: a CLI hiccup is a
    missing sample, not the end of the watch."""
    try:
        done = subprocess.run(["az"] + args, capture_output=True, text=True,
                              shell=False, timeout=120)
        return (done.stdout or "").strip()
    except Exception as exc:  # noqa: BLE001
        _log.LogPipeline("WARNING", "monitor: az %s failed: %s",
                         args[0] if args else "?", type(exc).__name__)
        return ""


def _az_json(args: list[str]):
    raw = _az(args)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        _log.LogPipeline("WARNING", "monitor: unparsable az output for %s",
                         " ".join(args[:3]))
        return None


class Sheet:
    """The spreadsheet: ONE WORKSHEET PER RUN, appended a row at a time and
    saved each sample so a kill never loses what was already measured.

    One shared tab would mix every run's machines and progress into a single
    column of timestamps, and comparing two runs -- which is the whole reason
    to keep the numbers -- would mean filtering by hand. A tab is named for
    the run it holds, so the workbook accumulates runs side by side.
    """

    def __init__(self, path: pathlib.Path) -> None:
        from openpyxl import Workbook, load_workbook  # noqa: PLC0415
        self._path = path
        self._load = load_workbook
        if path.is_file():
            self._book = load_workbook(path)
        else:
            self._book = Workbook()
            # a fresh Workbook arrives with one empty default sheet; the run
            # tabs are made on demand, so it would otherwise linger blank.
            self._book.remove(self._book.active)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._book.create_sheet("index")
            self._book["index"].append(["run_id", "first_sample_utc"])
            self._book.save(path)

    def _tab_for(self, run_id: str):
        """The run's own worksheet, made on first sight of that run."""
        name = (run_id or "unassigned")[:31]  # Excel caps a tab name at 31
        if name in self._book.sheetnames:
            return self._book[name]
        tab = self._book.create_sheet(name)
        tab.append(list(COLUMNS))
        tab.freeze_panes = "A2"
        for index, column in enumerate(COLUMNS, start=1):
            tab.column_dimensions[
                tab.cell(row=1, column=index).column_letter
            ].width = max(14, min(28, len(column) + 4))
        if "index" in self._book.sheetnames:
            self._book["index"].append([run_id or "unassigned",
                                        _utc_now_text()])
        return tab

    def append(self, row: dict) -> None:
        tab = self._tab_for(str(row.get("run_id") or ""))
        tab.append([row.get(name) for name in COLUMNS])
        self._book.save(self._path)

class Watch:
    def __init__(self, log_path: pathlib.Path, sheet_path: pathlib.Path,
                 interval_s: int) -> None:
        self._log_path = log_path
        self._interval_s = interval_s
        self._sheet = Sheet(sheet_path)
        self._mongo = None
        log_path.parent.mkdir(parents=True, exist_ok=True)

    def say(self, line: str) -> None:
        with self._log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{_utc_now_text()}  {line}\n")

    # ── the facts ────────────────────────────────────────────────────────────
    def job(self) -> dict:
        jobs = _az_json(["automation", "job", "list",
                         "--automation-account-name", AUTOMATION_ACCOUNT,
                         "--resource-group", RESOURCE_GROUP, "-o", "json"])
        if jobs is None:
            self.say("JOB    unreadable")
            return {}
        live = [j for j in jobs if str(j.get("status")) in
                ("New", "Activating", "Running", "Stopping", "Suspending")]
        if not live:
            ended = [j for j in jobs if j.get("endTime")]
            ended.sort(key=lambda j: str(j.get("endTime")), reverse=True)
            if ended:
                last = ended[0]
                self.say(f"JOB    none live; last {last.get('name')} "
                         f"{last.get('status')} at {last.get('endTime')}")
                return {"job_name": last.get("name"),
                        "job_status": f"ended:{last.get('status')}"}
            self.say("JOB    none live")
            return {}
        first = live[0]
        self.say(f"JOB    {first.get('name')} status={first.get('status')} "
                 f"runbook={first.get('runbookName')}")
        return {"job_name": first.get("name"),
                "job_status": first.get("status")}

    def run(self) -> dict:
        runs = self._mongo["pipelineAdmin"]["pipeline.runs"]
        doc = runs.find_one({"pipeline_name": PIPELINE_NAME,
                             "status": {"$nin": list(TERMINAL)}},
                            sort=[("started_at", -1)])
        if doc is None:
            doc = runs.find_one({"pipeline_name": PIPELINE_NAME},
                                sort=[("started_at", -1)])
            if doc is None:
                self.say("RUN    no record")
                return {}
            self.say(f"RUN    {doc.get('run_id')} TERMINAL "
                     f"{doc.get('status')} reason={doc.get('abort_reason')}")
            return {"run_id": doc.get("run_id"),
                    "run_status": doc.get("status")}
        steps = doc.get("step_transitions") or []
        last = steps[-1] if steps else {}
        self.say(f"RUN    {doc.get('run_id')} status={doc.get('status')} "
                 f"steps={len(steps)} last={last.get('step')}/"
                 f"{last.get('status')} hb={doc.get('controller_heartbeat_at')}")
        return {
            "run_id": doc.get("run_id"),
            "run_status": doc.get("status"),
            "steps_done": len(steps),
            "last_step": last.get("step"),
            "last_step_status": last.get("status"),
            "controller_heartbeat_at": str(
                doc.get("controller_heartbeat_at") or ""),
        }

    def work(self, run_id: str) -> dict:
        if not run_id:
            return {}
        coll = self._mongo["pipelineAdmin"]["pipeline.work_items"]
        tally: dict[str, int] = {}
        for doc in coll.find({"run_id": run_id}, {"status": 1, "step": 1}):
            status = str(doc.get("status") or "unknown")
            tally[status] = tally.get(status, 0) + 1
            key = f"{doc.get('step')}:{status}"
            tally[key] = tally.get(key, 0) + 1
        if not tally:
            self.say("WORK   none yet")
            return {}
        for key in sorted(k for k in tally if ":" in k):
            self.say(f"WORK   {key} = {tally[key]}")
        return {
            "work_pending": tally.get("pending", 0),
            "work_running": tally.get("running", 0),
            "work_done": tally.get("done", 0) + tally.get("complete", 0),
            "work_failed": tally.get("failed", 0),
        }

    def findings(self, run_id: str) -> dict:
        if not run_id:
            return {}
        coll = self._mongo["pipelineAdmin"]["discrepancyLog"]
        total = coll.find_one({"_id": f"{run_id}:run:total"}, {"count": 1})
        count = (total or {}).get("count")
        if count is not None:
            self.say(f"FIND   run total = {count}")
        for doc in coll.find({"run_id": run_id, "kind": "type_aggregate"},
                             {"class": 1, "count": 1, "severity": 1}):
            self.say(f"FIND   {doc.get('class')} sev={doc.get('severity')} "
                     f"count={doc.get('count')}")
        return {"findings_total": count}

    def machines(self) -> list[dict]:
        vms = _az_json(["vm", "list", "-g", RESOURCE_GROUP, "-o", "json"])
        if vms is None:
            self.say("VM     unreadable")
            return [{}]
        if not vms:
            self.say("VM     none in the resource group")
            return [{}]
        rows = []
        for vm in vms:
            name = vm.get("name")
            size = (vm.get("hardwareProfile") or {}).get("vmSize")
            self.say(f"VM     {name} size={size} "
                     f"provisioning={vm.get('provisioningState')}")
            row = {"vm_name": name, "vm_size": size,
                   "vm_provisioning": vm.get("provisioningState")}
            row.update(self._metrics(name, vm.get("id")))
            rows.append(row)
        return rows

    def _metrics(self, vm_name: str, vm_id: str) -> dict:
        out: dict = {}
        for metric in METRICS:
            payload = _az_json([
                "monitor", "metrics", "list", "--resource", str(vm_id),
                "--metric", metric, "--interval", "PT1M",
                "--aggregation", "Average", "Maximum", "--top", "1",
                "-o", "json"])
            average = maximum = None
            if payload is not None:
                for series in payload.get("value", []):
                    for stream in series.get("timeseries", []):
                        for point in stream.get("data", []):
                            if point.get("average") is not None:
                                average = point.get("average")
                            if point.get("maximum") is not None:
                                maximum = point.get("maximum")
            if average is None and maximum is None:
                self.say(f"METRIC {vm_name} {metric}: no data")
            else:
                self.say(f"METRIC {vm_name} {metric}: avg={average} "
                         f"max={maximum}")
            if metric == "Percentage CPU":
                out["cpu_avg_pct"] = average
                out["cpu_max_pct"] = maximum
            elif metric == "Available Memory Bytes":
                out["available_memory_bytes"] = average
            elif metric == "Network In Total":
                out["network_in_total"] = maximum
            else:
                out["os_disk_iops_pct"] = maximum
        return out

    # ── the loop ─────────────────────────────────────────────────────────────
    def forever(self) -> int:
        self.say("=" * 72)
        self.say(f"MONITOR start -- every {self._interval_s}s")
        _log.LogPipeline("INFO", "medicare_monitor: watching every %ds",
                         self._interval_s)
        while True:
            try:
                if self._mongo is None:
                    self._mongo = ChatHealthyMongoUtilities().getConnection(
                        "pipelineEditor", "ChatHealthyFrontEnd")
                base = {"sampled_at_utc": _utc_now_text()}
                base.update(self.job())
                run = self.run()
                base.update(run)
                run_id = str(run.get("run_id") or "")
                base.update(self.work(run_id))
                base.update(self.findings(run_id))
                for machine in self.machines():
                    row = dict(base)
                    row.update(machine)
                    self._sheet.append(row)
                self.say("-" * 48)
            except Exception as exc:  # noqa: BLE001
                self.say(f"SAMPLE failed: {type(exc).__name__}: "
                         f"{str(exc)[:200]}")
                _log.LogPipeline("WARNING", "medicare_monitor: sample failed "
                                 "(%s); continuing", type(exc).__name__)
                self._mongo = None
            time.sleep(self._interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Watch a Medicare run and its machines.")
    parser.add_argument("--log", default="_oneshots/medicare_status.log",
                        help="text log of every sample")
    parser.add_argument("--sheet", default="_oneshots/medicare_run_metrics.xlsx",
                        help="spreadsheet, one row per sample per machine")
    parser.add_argument("--interval-seconds", dest="interval_s", type=int,
                        default=60)
    args = parser.parse_args(argv)
    if args.interval_s < 10:
        raise ChatHealthyException(
            mode="value_error", component="medicare_monitor",
            message="--interval-seconds below 10 samples faster than Azure "
                    "Monitor publishes, so it measures nothing new and costs "
                    "API calls.")
    watch = Watch(pathlib.Path(args.log), pathlib.Path(args.sheet),
                  args.interval_s)
    return watch.forever()


if __name__ == "__main__":
    sys.exit(main())
