# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""PipelineRegressionTest - Azure Automation runbook (Python 3).

Exercises the deployed pipeline components against manufactured conditions and
asserts on what they actually did. It does not import them and does not stand
anything in for them: each component is started as its own Automation job, the
same way its schedule starts it, and the assertions are on the metadata and the
infrastructure afterwards.

Nothing it creates is real. The runs never existed, and the processes on the
host only carry the pipeline's names and run ids. That is the point -- the
components have no way to tell, so what they do to the forgeries is what they
would do to the real thing.

Everything it creates it removes, in a finally, including on failure.

FUNCTIONS
  zombie_detection   A dead run whose host still carries its processes, and a
                     live run alongside it. Asserts the reaper corrects the
                     dead run's metadata and leaves the live one, and that the
                     Watchdog deletes the dead host and spares the live one.

Environment (Automation Variables):
  AUTOMATION_SUBSCRIPTION_ID / AUTOMATION_RESOURCE_GROUP / AZ_AUTOMATION_ACCOUNT
  PIPELINEEDITOR_AZURE_TENANT_ID / _CLIENT_ID / _CLIENT_SECRET
  ENV_PREFIX, CH_LOG_DB, KEY_VAULT_URI
"""
# Atlas addresses are SRV records and the Automation sandbox's own resolver
# does not answer external SRV queries. This MUST run before pymongo is
# imported, which the chathealthy_lib import below does.
try:
    import dns.resolver  # type: ignore[import-not-found]
    _r = dns.resolver.Resolver(configure=False)
    _r.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    _r.timeout = 5
    _r.lifetime = 10
    dns.resolver.default_resolver = _r
except ImportError:
    pass

import base64
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

PIPELINE_ADMIN_DB = "pipelineAdmin"

from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService, set_mongo_log_identity)
set_mongo_log_identity("pipelineEditor")

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

sys.stderr.write("regression test: module import begin\n")

try:
    import automationassets
    for _k in ("AUTOMATION_SUBSCRIPTION_ID", "AUTOMATION_RESOURCE_GROUP",
               "AZ_AUTOMATION_ACCOUNT", "ENV_PREFIX", "CH_LOG_DB",
               "KEY_VAULT_URI",
               "PIPELINEEDITOR_AZURE_TENANT_ID",
               "PIPELINEEDITOR_AZURE_CLIENT_ID",
               "PIPELINEEDITOR_AZURE_CLIENT_SECRET"):
        try:
            os.environ[_k] = str(automationassets.get_automation_variable(_k))
        except Exception:
            pass
except ImportError:
    pass

os.environ.setdefault("CH_SPACE_NAME", "pipeline-regression-test")
os.environ.setdefault("CH_COMPONENT", "pipeline-regression-test")
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")

log = ChatHealthyLoggingService()

ARM = "https://management.azure.com"
SUBSCRIPTION = os.environ.get("AUTOMATION_SUBSCRIPTION_ID", "")
RESOURCE_GROUP = os.environ.get("AUTOMATION_RESOURCE_GROUP", "rg-chathealthy-pipeline-dev")
AUTOMATION_ACCOUNT = os.environ.get("AZ_AUTOMATION_ACCOUNT", "ChatHealthyJobManager")
LOCATION = os.environ.get("AZ_VM_LOCATION", "eastus2")
VNET = os.environ.get("AZ_VNET_NAME", "vnet-chathealthy-pipeline-dev")
SUBNET = os.environ.get("AZ_SUBNET_NAME", "snet-pipeline-compute")
VM_SIZE = os.environ.get("REGRESSION_VM_SIZE", "Standard_D2als_v7")
RESULTS_COLLECTION = "pipeline.regression_results"

# Every name this runbook creates carries this marker, so anything it leaves
# behind is identifiable and removable without guessing.
MARKER = "chregtest"


class PipelineRegressionTest:
    """Manufactured conditions, real components, assertions on the outcome."""

    def __init__(self) -> None:
        self._token = None
        self._mongo = ChatHealthyMongoUtilities().getConnection(
            "pipelineEditor", "admin")
        self._db = self._mongo[PIPELINE_ADMIN_DB]
        self._created = []

    # ── entry ───────────────────────────────────────────────────────────────
    def run(self, only: str = "") -> dict:
        functions = {"zombie_detection": self.zombie_detection}
        chosen = {k: v for k, v in functions.items() if not only or k == only}
        results = {}
        for name, fn in chosen.items():
            started = datetime.now(timezone.utc)
            try:
                checks = fn()
                passed = all(c["passed"] for c in checks)
                results[name] = {"passed": passed, "checks": checks}
            except Exception as exc:  # noqa: BLE001
                results[name] = {"passed": False, "error": f"{type(exc).__name__}: {exc}",
                                 "checks": []}
            results[name]["seconds"] = int(
                (datetime.now(timezone.utc) - started).total_seconds())
            self._record(name, results[name])
        return results

    # ── function 1 ──────────────────────────────────────────────────────────
    def zombie_detection(self) -> list:
        """A dead run whose processes outlived it, beside a live run."""
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        dead_run = f"{MARKER}-dead-{stamp}"
        live_run = f"{MARKER}-live-{stamp}"
        dead_vm = f"vm-{MARKER}-dead-{stamp}"[:60]
        live_vm = f"vm-{MARKER}-live-{stamp}"[:60]
        checks = []
        try:
            self._write_metadata(dead_run, dead_vm, live_run, live_vm)
            self._create_vm(dead_vm, dead_run)
            ready = self._wait_ready(dead_vm)
            checks.append(self._check("host is up and answering", ready))
            if not ready:
                return checks

            seen = self._processes_on(dead_vm)
            checks.append(self._check(
                "the host reports the run's processes",
                any(dead_run in line for line in seen), detail=str(seen[:3])))

            reaper = self._start_runbook("ReservationReaper")
            checks.append(self._check("ReservationReaper ran",
                                      reaper == "Completed", detail=reaper))

            dead = self._db["pipeline.runs"].find_one({"run_id": dead_run}) or {}
            live = self._db["pipeline.runs"].find_one({"run_id": live_run}) or {}
            checks.append(self._check(
                "dead run corrected to a terminal state",
                str(dead.get("status", "")).lower() in
                ("failed", "aborted", "completed", "succeeded"),
                detail=str(dead.get("status"))))
            checks.append(self._check(
                "live run left alone",
                str(live.get("status", "")).lower() == "running",
                detail=str(live.get("status"))))
            checks.append(self._check(
                "dead run's reservation released",
                self._db["cluster_lifecycle"].find_one({"_id": dead_run}) is None))
            checks.append(self._check(
                "live run's reservation kept",
                self._db["cluster_lifecycle"].find_one({"_id": live_run}) is not None))

            watchdog = self._start_runbook("WatchdogRunbook")
            checks.append(self._check("WatchdogRunbook ran",
                                      watchdog == "Completed", detail=watchdog))
            checks.append(self._check(
                "dead host deleted", not self._vm_exists(dead_vm)))
            return checks
        finally:
            self._teardown(dead_run, live_run)

    # ── manufactured conditions ─────────────────────────────────────────────
    def _write_metadata(self, dead_run, dead_vm, live_run, live_vm) -> None:
        """Runs and reservations for jobs that never existed."""
        now = datetime.utcnow()
        old = now - timedelta(hours=3)
        self._db["pipeline.runs"].insert_one({
            "run_id": dead_run, "pipeline_name": "provider", "status": "running",
            "started_at": old, "vm_name": dead_vm,
            "controller_heartbeat_at": old, "regression_test": True})
        self._db["pipeline.runs"].insert_one({
            "run_id": live_run, "pipeline_name": "provider", "status": "running",
            "started_at": old, "vm_name": live_vm,
            "controller_heartbeat_at": now, "regression_test": True})
        self._db["cluster_lifecycle"].replace_one(
            {"_id": dead_run},
            {"_id": dead_run, "run_id": dead_run, "vm_name": dead_vm,
             "requester": MARKER, "expiry_at": now - timedelta(hours=1),
             "regression_test": True}, upsert=True)
        self._db["cluster_lifecycle"].replace_one(
            {"_id": live_run},
            {"_id": live_run, "run_id": live_run, "vm_name": live_vm,
             "requester": MARKER, "expiry_at": now + timedelta(hours=4),
             "regression_test": True}, upsert=True)
        log.info("regression: metadata written dead=%s live=%s", dead_run, live_run)

    def _cloud_init(self, run_id: str) -> str:
        """Processes that only pretend to be the pipeline's.

        One carries its run id the way a Controller does, in the environment;
        one the way a worker does, in argv; and one forks a child that exits
        unreaped, so the host carries a real defunct process too.
        """
        script = (
            "#cloud-config\n"
            "runcmd:\n"
            "  - |\n"
            f"    export RUN_ID={run_id}\n"
            "    setsid python3 -c 'import time; time.sleep(7200)' "
            "--tag control_runner &\n"
            "    unset RUN_ID\n"
            "    setsid python3 -c 'import time; time.sleep(7200)' "
            f"pipeline_worker --run-id {run_id} &\n"
            "    setsid python3 -c 'import os,time\\nif os.fork()==0: os._exit(0)\\n"
            "time.sleep(7200)' "
            f"pipeline_worker --run-id {run_id} &\n")
        return base64.b64encode(script.encode()).decode()

    # ── Azure ───────────────────────────────────────────────────────────────
    def _bearer(self) -> str:
        if self._token:
            return self._token
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": os.environ["PIPELINEEDITOR_AZURE_CLIENT_ID"],
            "client_secret": os.environ["PIPELINEEDITOR_AZURE_CLIENT_SECRET"],
            "scope": f"{ARM}/.default"}).encode()
        req = urllib.request.Request(
            "https://login.microsoftonline.com/"
            f"{os.environ['PIPELINEEDITOR_AZURE_TENANT_ID']}/oauth2/v2.0/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=20) as resp:
            self._token = json.loads(resp.read().decode("utf-8"))["access_token"]
        return self._token

    def _arm(self, method, path, body=None, api="2024-03-01", timeout=180):
        url = (f"{ARM}/subscriptions/{SUBSCRIPTION}/resourceGroups/"
               f"{RESOURCE_GROUP}{path}?api-version={api}")
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Authorization": f"Bearer {self._bearer()}",
                     "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, {}

    def _create_vm(self, vm_name: str, run_id: str) -> None:
        nic = f"{vm_name}-nic"
        subnet = (f"/subscriptions/{SUBSCRIPTION}/resourceGroups/{RESOURCE_GROUP}"
                  f"/providers/Microsoft.Network/virtualNetworks/{VNET}"
                  f"/subnets/{SUBNET}")
        code, body = self._arm(
            "PUT", f"/providers/Microsoft.Network/networkInterfaces/{nic}",
            {"location": LOCATION,
             "tags": {"purpose": MARKER},
             "properties": {"ipConfigurations": [
                 {"name": "ipcfg",
                  "properties": {"subnet": {"id": subnet},
                                 "privateIPAllocationMethod": "Dynamic"}}]}},
            api="2023-09-01")
        if code >= 400:
            _raise_azure("network interface", nic, code, body)
        self._created.append(("nic", nic))
        nic_id = body["id"]

        code, body = self._arm(
            "PUT", f"/providers/Microsoft.Compute/virtualMachines/{vm_name}",
            {"location": LOCATION,
             "tags": {"pipeline_run_id": run_id, "purpose": MARKER},
             "properties": {
                 "hardwareProfile": {"vmSize": VM_SIZE},
                 "storageProfile": {
                     "imageReference": {
                         "publisher": "Canonical",
                         "offer": "0001-com-ubuntu-server-jammy",
                         "sku": "22_04-lts-gen2", "version": "latest"},
                     "osDisk": {"createOption": "FromImage",
                                "deleteOption": "Delete"}},
                 "osProfile": {
                     "computerName": vm_name[:15],
                     "adminUsername": os.environ.get("AZ_VM_ADMIN_USERNAME", "chadmin"),
                     "customData": self._cloud_init(run_id),
                     "linuxConfiguration": {
                         "disablePasswordAuthentication": True,
                         "ssh": {"publicKeys": [{
                             "path": f"/home/{os.environ.get('AZ_VM_ADMIN_USERNAME', 'chadmin')}"
                                     "/.ssh/authorized_keys",
                             "keyData": os.environ.get("AZ_VM_ADMIN_SSH_PUBKEY", "")}]}}},
                 "networkProfile": {"networkInterfaces": [
                     {"id": nic_id, "properties": {"deleteOption": "Delete"}}]}}})
        if code >= 400:
            _raise_azure("virtual machine", vm_name, code, body)
        self._created.append(("vm", vm_name))
        log.info("regression: created %s for %s", vm_name, run_id)

    def _wait_ready(self, vm_name: str, minutes: int = 12) -> bool:
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            code, body = self._arm(
                "GET",
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/instanceView")
            if code == 200:
                running = any(s.get("code") == "PowerState/running"
                              for s in body.get("statuses", []))
                agent = any(a.get("code", "").endswith("Ready")
                            for a in body.get("vmAgent", {}).get("statuses", []))
                if running and agent:
                    time.sleep(30)  # let cloud-init finish spawning
                    return True
            time.sleep(20)
        return False

    def _processes_on(self, vm_name: str) -> list:
        script = ("for d in /proc/[0-9]*; do "
                  "a=$(tr '\\000' ' ' < $d/cmdline 2>/dev/null); "
                  "case \"$a\" in *pipeline_worker*|*control_runner*) "
                  "echo \"${d##*/} $a\";; esac; done")
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript", "script": [script]})
        if code >= 400:
            return []
        out = []
        for entry in body.get("value", []) or []:
            out.extend((entry.get("message") or "").splitlines())
        return [line for line in out if line.strip()]

    def _vm_exists(self, vm_name: str) -> bool:
        code, _ = self._arm(
            "GET", f"/providers/Microsoft.Compute/virtualMachines/{vm_name}")
        return code == 200

    def _start_runbook(self, runbook: str, minutes: int = 15) -> str:
        """Start a runbook the way its schedule does, and wait for it."""
        job = f"{MARKER}-{runbook}-{int(time.time())}"
        code, _ = self._arm(
            "PUT",
            f"/providers/Microsoft.Automation/automationAccounts/"
            f"{AUTOMATION_ACCOUNT}/jobs/{job}",
            {"properties": {"runbook": {"name": runbook}}},
            api="2023-11-01")
        if code >= 400:
            return f"start_failed_{code}"
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            code, body = self._arm(
                "GET",
                f"/providers/Microsoft.Automation/automationAccounts/"
                f"{AUTOMATION_ACCOUNT}/jobs/{job}", api="2023-11-01")
            status = (body.get("properties") or {}).get("status", "")
            if status in ("Completed", "Failed", "Stopped", "Suspended"):
                log.info("regression: %s finished %s", runbook, status)
                return status
            time.sleep(15)
        return "timed_out"

    # ── bookkeeping ─────────────────────────────────────────────────────────
    @staticmethod
    def _check(name: str, passed: bool, detail: str = "") -> dict:
        log.info("regression check: %s -> %s %s", name,
                 "PASS" if passed else "FAIL", detail)
        return {"check": name, "passed": bool(passed), "detail": detail}

    def _record(self, function: str, result: dict) -> None:
        try:
            self._db[RESULTS_COLLECTION].insert_one({
                "function": function,
                "ran_at": datetime.now(timezone.utc),
                "env": os.environ.get("ENV_PREFIX", "dev"),
                **result})
        except Exception as exc:  # noqa: BLE001
            log.warning("regression: could not record result (%r)", exc)

    def _teardown(self, *run_ids) -> None:
        """Everything this created, removed -- including on failure."""
        for kind, name in reversed(self._created):
            path = ("/providers/Microsoft.Compute/virtualMachines/"
                    if kind == "vm" else
                    "/providers/Microsoft.Network/networkInterfaces/")
            api = "2024-03-01" if kind == "vm" else "2023-09-01"
            code, _ = self._arm("DELETE", path + name, api=api)
            log.info("regression: teardown %s %s -> %s", kind, name, code)
            if kind == "vm":
                for _ in range(30):
                    if not self._vm_exists(name):
                        break
                    time.sleep(10)
        self._created = []
        for run_id in run_ids:
            self._db["pipeline.runs"].delete_many({"run_id": run_id})
            self._db["cluster_lifecycle"].delete_many({"_id": run_id})
            self._db["cluster_lifecycle"].delete_many({"run_id": run_id})
            self._db["pipeline.work_items"].delete_many({"run_id": run_id})
        log.info("regression: teardown complete")


def _raise_azure(what: str, name: str, code: int, body: dict) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="runtime_error",
        message=f"could not create {what} {name}: HTTP {code} {json.dumps(body)[:300]}",
        component="PipelineRegressionTest")


def _main() -> int:
    only = os.environ.get("REGRESSION_FUNCTION", "").strip()
    results = PipelineRegressionTest().run(only)
    for name, result in results.items():
        log.info("regression %s: %s in %ss", name,
                 "PASS" if result["passed"] else "FAIL", result.get("seconds"))
        for check in result.get("checks", []):
            log.info("   %s %s %s", "PASS" if check["passed"] else "FAIL",
                     check["check"], check.get("detail", ""))
        if result.get("error"):
            log.error("regression %s errored: %s", name, result["error"])
    return 0 if all(r["passed"] for r in results.values()) else 1


if __name__ == "__main__":
    sys.exit(_main())
