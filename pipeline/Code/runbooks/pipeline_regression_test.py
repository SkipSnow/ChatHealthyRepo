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
               "KEY_VAULT_URI", "PIPELINE_CLUSTER",
               # The VM this creates needs an admin name and a public key, and
               # a VM PUT with an empty keyData is rejected outright. These
               # were absent from this list, so the first run died on
               # "linuxConfiguration.ssh.publicKeys.keyData is invalid".
               "AZ_VM_ADMIN_USERNAME", "AZ_VM_ADMIN_SSH_PUBKEY",
               "AZ_VM_LOCATION", "AZ_VM_RESOURCE_GROUP",
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
HOST_LOG = "/var/log/chregtest.log"

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
        self._host_logs = {}

    # -- entry ---------------------------------------------------------------
    def run(self, only: str = "") -> dict:
        functions = {"zombie_detection": self.zombie_detection,
                     "user_reservations": self.user_reservations}
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

    # -- function 1 ----------------------------------------------------------
    def zombie_detection(self) -> list:
        """A dead run whose processes outlived it, beside a live run."""
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        dead_run = f"{MARKER}-provider-dead-{stamp}"
        live_run = f"{MARKER}-testpipeline-live-{stamp}"
        dead_vm = f"vm-{MARKER}-provider-{stamp}"[:60]
        live_vm = f"vm-{MARKER}-testpipe-{stamp}"[:60]
        checks = []
        try:
            self._write_metadata(dead_run, dead_vm, live_run, live_vm)
            # Both runs get a host. Naming a host for the live run without
            # creating it made ARM answer 404, which the components correctly
            # read as "this run is over" -- so the assertion that the live run
            # is spared would have failed while they behaved perfectly, and
            # argued for teaching them to leave dead runs alone.
            self._create_vm(dead_vm, dead_run, controller_seconds=20)
            self._create_vm(live_vm, live_run, controller_seconds=7200)
            dead_ready = self._wait_ready(dead_vm)
            live_ready = self._wait_ready(live_vm)
            for vm in (dead_vm, live_vm):
                self._log_host(vm)
            detail = "" if (dead_ready and live_ready) else (
                f"{dead_vm}={dead_ready} {live_vm}={live_ready}")
            checks.append(self._check("both hosts are up and answering",
                                      dead_ready and live_ready, detail))
            if not (dead_ready and live_ready):
                return checks

            # The pid the host actually got, written where the components
            # look for it. Without it verdict() reads "no pid recorded" and
            # answers RUNNING for everything, which is why no forgery this
            # test made was ever eligible to be reaped.
            for run, vm in ((dead_run, dead_vm), (live_run, live_vm)):
                pid = self._controller_pid(vm)
                if pid is None:
                    checks.append(self._check(
                        "the host recorded its controller pid", False,
                        detail=f"{vm}: no pid line in the host log"))
                    return checks
                self._db["pipeline.runs"].update_one(
                    {"run_id": run}, {"$set": {"controller_pid": pid}})
                self._db["cluster_lifecycle"].update_one(
                    {"_id": run}, {"$set": {"controller_pid": pid}})
                log.info("regression: %s controller_pid=%s", run, pid)
            checks.append(self._check("the host recorded its controller pid", True))

            dead_pid_gone = not self._pid_alive(dead_vm, self._controller_pid(dead_vm))
            checks.append(self._check(
                "the dead run's controller has exited", dead_pid_gone,
                detail=f"pid {self._controller_pid(dead_vm)} on {dead_vm}"))
            live_pid_alive = self._pid_alive(live_vm, self._controller_pid(live_vm))
            checks.append(self._check(
                "the live run's controller is still up", live_pid_alive,
                detail=f"pid {self._controller_pid(live_vm)} on {live_vm}"))

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
            checks.append(self._check(
                "live host spared", self._vm_exists(live_vm)))
            live_procs = self._processes_on(live_vm)
            checks.append(self._check(
                "live host's processes still running",
                any(live_run in line for line in live_procs),
                detail=str(live_procs[:2])))
            return checks
        finally:
            self._teardown(dead_run, live_run)

    # -- manufactured conditions ---------------------------------------------
    def user_reservations(self) -> list:
        """A reservation a person holds, and one whose time is up.

        A user reservation is a row and nothing else -- no host, no process --
        so this is decided entirely in the database. Two of them are written,
        one still in date and one lapsed, and the reaper is asked to judge
        both in the same tick.
        """
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        held = f"{MARKER}-user-held-{stamp}"
        lapsed = f"{MARKER}-user-lapsed-{stamp}"
        now = datetime.utcnow()
        checks = []
        try:
            self._db["cluster_lifecycle"].replace_one(
                {"_id": held},
                {"_id": held, "job_id": held, "requester": MARKER,
                 "cluster_name": os.environ.get("PIPELINE_CLUSTER",
                                                "ChatHealthyDataPipelines"),
                 "status": "active", "reservation_class": "human",
                 "start_time": (now - timedelta(minutes=5)).isoformat(),
                 "expiry_at": now + timedelta(hours=3),
                 "regression_test": True}, upsert=True)
            self._db["cluster_lifecycle"].replace_one(
                {"_id": lapsed},
                {"_id": lapsed, "job_id": lapsed, "requester": MARKER,
                 "cluster_name": os.environ.get("PIPELINE_CLUSTER",
                                                "ChatHealthyDataPipelines"),
                 "status": "active", "reservation_class": "human",
                 "start_time": (now - timedelta(hours=4)).isoformat(),
                 "expiry_at": now - timedelta(hours=1),
                 "regression_test": True}, upsert=True)
            log.info("regression: user reservations written held=%s lapsed=%s",
                     held, lapsed)

            status = self._start_runbook("ReservationReaper")
            checks.append(self._check("ReservationReaper ran",
                                      status == "Completed", detail=status))
            checks.append(self._check(
                "a reservation still in date is respected",
                self._db["cluster_lifecycle"].find_one({"_id": held}) is not None))
            checks.append(self._check(
                "a lapsed reservation is reaped",
                self._db["cluster_lifecycle"].find_one({"_id": lapsed}) is None))
            return checks
        finally:
            self._db["cluster_lifecycle"].delete_many({"_id": held})
            self._db["cluster_lifecycle"].delete_many({"_id": lapsed})
            log.info("regression: user reservations cleaned up")

    def _write_metadata(self, dead_run, dead_vm, live_run, live_vm) -> None:
        """Runs and reservations for jobs that never existed."""
        now = datetime.utcnow()
        old = now - timedelta(hours=3)
        self._db["pipeline.runs"].insert_one({
            "run_id": dead_run, "pipeline_name": "provider", "status": "running",
            "started_at": old, "vm_name": dead_vm,
            "controller_heartbeat_at": old, "regression_test": True})
        # Two runs, two pipelines. The lock is keyed pipeline_lock:<name>, so
        # two live runs of the SAME pipeline cannot occur and asserting on
        # that state would assert on a fiction. testPipeline is this runbook's
        # own name -- it owns no orchestrator and loads nothing; it exists so
        # the concurrent shape is real, and so a dead run in one pipeline can
        # be shown not to cost a live run in another its host.
        self._db["pipeline.runs"].insert_one({
            "run_id": live_run, "pipeline_name": "testPipeline", "status": "running",
            "started_at": old, "vm_name": live_vm,
            "controller_heartbeat_at": now, "regression_test": True})
        log.info("regression: live run %s heartbeat is fresh", live_run)
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

    def _cloud_init(self, run_id: str, controller_seconds: int = 7200) -> str:
        """Processes that only pretend to be the pipeline's.

        controller_seconds is what makes a run dead or alive. These components
        decide by kill -0 on the recorded controller_pid, so a dead run is one
        whose controller has exited while its workers and its defunct child
        are still on the host. Nothing else expresses that: a stale heartbeat
        does not, and the earlier forgery, which left a live controller on the
        dead host, described a healthy run the components were right to spare.

        One carries its run id the way a Controller does, in the environment;
        one the way a worker does, in argv; and one forks a child that exits
        unreaped, so the host carries a real defunct process too.

        Each spawn is recorded on the host itself, with its pid, before any
        component is asked about it. Without that record an empty process
        list is ambiguous: it reads the same whether the watchdog killed
        them or cloud-init never started them, and one of those is the
        thing under test.
        """
        script = (
            "#cloud-config\n"
            "runcmd:\n"
            "  - |\n"
            f"    L={HOST_LOG}\n"
            "    say() { echo \"$(date -u +%Y-%m-%dT%H:%M:%SZ) $*\" >> $L; }\n"
            f"    say \"boot run_id={run_id}\"\n"
            "    say \"python3=$(command -v python3 || echo MISSING)\"\n"
            # setsid puts each process in its own session so runcmd returns
            # at once. Without it one host of two hung in
            # osProvisioningInProgress until Azure gave up: provisioning on
            # these images is signalled by cloud-init finishing, so anything
            # holding runcmd open fails the machine.
            #
            # setsid may fork, which makes $! its pid and not the pid of the
            # process it started, so each process writes its own. The
            # components ask kill -0 about that number.
            f"    export RUN_ID={run_id}\n"
            "    setsid python3 -c \"import os,time; "
            "open('/var/log/chregtest.log','a')"
            ".write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + "
            "' spawned controller pid=' + str(os.getpid()) + chr(10)); "
            f"time.sleep({controller_seconds})\" "
            "--tag control_runner >/dev/null 2>&1 &\n"
            "    unset RUN_ID\n"
            "    setsid python3 -c \"import os,time; open('/var/log/chregtest.log','a').write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' spawned worker pid=' + str(os.getpid()) + chr(10)); time.sleep(7200)\" "
            f"pipeline_worker --run-id {run_id} >/dev/null 2>&1 &\n"
            # One statement, no escaped newline. Written with a backslash-n
            # it reached python3 -c literally, died on a SyntaxError before
            # forking, and left the host with no defunct process at all.
            "    setsid python3 -c \"import os,time; open('/var/log/chregtest.log','a').write(time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()) + ' spawned forker pid=' + str(os.getpid()) + chr(10)); os._exit(0) if os.fork()==0 else time.sleep(7200)\" "
            f"pipeline_worker --run-id {run_id} >/dev/null 2>&1 &\n"
            "    sleep 6\n"
            "    say \"alive: $(ls -d /proc/[0-9]* | wc -l) procs\"\n"
            "    z=0\n"
            "    for d in /proc/[0-9]*; do\n"
            "      st=$(awk '{print $3}' $d/stat 2>/dev/null)\n"
            "      [ \"$st\" = \"Z\" ] && z=$((z+1))\n"
            "    done\n"
            "    say \"defunct: $z\"\n"
            "    say boot complete\n")
        return base64.b64encode(script.encode()).decode()

    def _controller_pid(self, vm_name: str):
        """The controller pid the host wrote down, or None."""
        for line in self._host_logs.get(vm_name, []):
            if "spawned controller pid=" in line:
                tail = line.split("spawned controller pid=", 1)[1].strip()
                digits = ""
                for ch in tail:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    return int(digits)
        return None

    def _pid_alive(self, vm_name: str, pid) -> bool:
        """kill -0, the same question the components ask."""
        if pid is None:
            return False
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript",
             "script": [f"kill -0 {int(pid)} 2>/dev/null && echo RUNNING || echo GONE"]})
        if code >= 400:
            return False
        for entry in body.get("value", []) or []:
            if "RUNNING" in (entry.get("message") or ""):
                return True
        return False

    def _host_log(self, vm_name: str) -> list:
        """Read back what the host recorded about itself."""
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript",
             "script": [f"cat {HOST_LOG} 2>/dev/null || echo 'NO HOST LOG'; "
                        f"echo '-- cloud-init --'; "
                        f"tail -n 15 /var/log/cloud-init-output.log 2>/dev/null"]})
        if code >= 400:
            return [f"could not read host log: HTTP {code}"]
        lines = []
        for entry in body.get("value", []) or []:
            lines.extend((entry.get("message") or "").splitlines())
        return [ln for ln in lines if ln.strip()]

    def _log_host(self, vm_name: str) -> None:
        """Put the host's own account of itself into the run's record."""
        lines = self._host_log(vm_name)
        self._host_logs[vm_name] = lines
        for line in lines:
            log.info("regression host %s | %s", vm_name, line)

    # -- Azure ---------------------------------------------------------------
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

    def _create_vm(self, vm_name: str, run_id: str,
                   controller_seconds: int = 7200) -> None:
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

        admin_user = os.environ.get("AZ_VM_ADMIN_USERNAME", "").strip()
        admin_key = os.environ.get("AZ_VM_ADMIN_SSH_PUBKEY", "").strip()
        if not admin_user or not admin_key:
            _raise_azure("virtual machine", vm_name, 0,
                         {"error": "AZ_VM_ADMIN_USERNAME or AZ_VM_ADMIN_SSH_PUBKEY "
                                   "is not set; a VM PUT with an empty key is "
                                   "rejected by Azure"})
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
                     "adminUsername": admin_user,
                     "customData": self._cloud_init(run_id, controller_seconds),
                     "linuxConfiguration": {
                         "disablePasswordAuthentication": True,
                         "ssh": {"publicKeys": [{
                             "path": f"/home/{admin_user}/.ssh/authorized_keys",
                             "keyData": admin_key}]}}},
                 "networkProfile": {"networkInterfaces": [
                     {"id": nic_id, "properties": {"deleteOption": "Delete"}}]}}})
        if code >= 400:
            _raise_azure("virtual machine", vm_name, code, body)
        self._created.append(("vm", vm_name))
        log.info("regression: created %s for %s", vm_name, run_id)

    def _vm_state(self, vm_name: str) -> str:
        """Provisioning and agent state, for the log."""
        code, body = self._arm(
            "GET",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/instanceView")
        if code >= 400:
            return f"instanceView HTTP {code}"
        power = ",".join(s.get("code", "") for s in body.get("statuses", []) or [])
        agent = (body.get("vmAgent") or {}).get("statuses") or []
        return f"{power} agent={agent[0].get('displayStatus') if agent else 'none'}"

    def _wait_ready(self, vm_name: str, minutes: int = 8) -> bool:
        """Poll the cheap call; make the expensive one once.

        instanceView is a GET that answers in about a second whatever state
        the host is in. runCommand against a host whose agent is not up does
        not answer at all -- it blocks until its timeout -- so polling with it
        cost 180 seconds per attempt and reported nothing in between.

        The agent's status carries code 'ProvisioningState/succeeded' and says
        Ready in displayStatus. Reading the code field instead cost a full
        twelve-minute wait against two healthy hosts.
        """
        deadline = time.time() + minutes * 60
        started = time.time()
        while time.time() < deadline:
            code, body = self._arm(
                "GET",
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/instanceView",
                timeout=30)
            power = ",".join(s.get("code", "") for s in body.get("statuses", []) or [])
            agent = (body.get("vmAgent") or {}).get("statuses") or []
            ready = any(a.get("displayStatus") == "Ready" for a in agent)
            waited = int(time.time() - started)
            if code < 400 and ready:
                answered = self._answers(vm_name)
                log.info("regression: %s agent Ready at %ds, answers=%s",
                         vm_name, waited, answered)
                return answered
            log.info("regression: waiting on %s %ds/%ds -- HTTP %s %s agent=%s",
                     vm_name, waited, minutes * 60, code, power,
                     agent[0].get("displayStatus") if agent else "none")
            time.sleep(10)
        log.info("regression: %s never became ready in %d minutes",
                 vm_name, minutes)
        return False

    def _answers(self, vm_name: str) -> bool:
        """One run command, once the agent says it can serve it."""
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript", "script": ["echo ready"]},
            timeout=120)
        if code >= 400:
            log.info("regression: %s run command HTTP %s", vm_name, code)
            return False
        for entry in body.get("value", []) or []:
            if "ready" in (entry.get("message") or ""):
                return True
        return False

    def _controller_pid(self, vm_name: str):
        """The controller pid the host wrote down, or None."""
        for line in self._host_logs.get(vm_name, []):
            if "spawned controller pid=" in line:
                tail = line.split("spawned controller pid=", 1)[1].strip()
                digits = ""
                for ch in tail:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    return int(digits)
        return None

    def _pid_alive(self, vm_name: str, pid) -> bool:
        """kill -0, the same question the components ask."""
        if pid is None:
            return False
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript",
             "script": [f"kill -0 {int(pid)} 2>/dev/null && echo RUNNING || echo GONE"]})
        if code >= 400:
            return False
        for entry in body.get("value", []) or []:
            if "RUNNING" in (entry.get("message") or ""):
                return True
        return False

    def _host_log(self, vm_name: str) -> list:
        """Read back what the host recorded about itself."""
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript",
             "script": [f"cat {HOST_LOG} 2>/dev/null || echo 'NO HOST LOG'; "
                        f"echo '-- cloud-init --'; "
                        f"tail -n 15 /var/log/cloud-init-output.log 2>/dev/null"]})
        if code >= 400:
            return [f"could not read host log: HTTP {code}"]
        lines = []
        for entry in body.get("value", []) or []:
            lines.extend((entry.get("message") or "").splitlines())
        return [ln for ln in lines if ln.strip()]

    def _log_host(self, vm_name: str) -> None:
        """Put the host's own account of itself into the run's record."""
        lines = self._host_log(vm_name)
        self._host_logs[vm_name] = lines
        for line in lines:
            log.info("regression host %s | %s", vm_name, line)

    # -- Azure ---------------------------------------------------------------
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

    def _create_vm(self, vm_name: str, run_id: str,
                   controller_seconds: int = 7200) -> None:
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

        admin_user = os.environ.get("AZ_VM_ADMIN_USERNAME", "").strip()
        admin_key = os.environ.get("AZ_VM_ADMIN_SSH_PUBKEY", "").strip()
        if not admin_user or not admin_key:
            _raise_azure("virtual machine", vm_name, 0,
                         {"error": "AZ_VM_ADMIN_USERNAME or AZ_VM_ADMIN_SSH_PUBKEY "
                                   "is not set; a VM PUT with an empty key is "
                                   "rejected by Azure"})
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
                     "adminUsername": admin_user,
                     "customData": self._cloud_init(run_id, controller_seconds),
                     "linuxConfiguration": {
                         "disablePasswordAuthentication": True,
                         "ssh": {"publicKeys": [{
                             "path": f"/home/{admin_user}/.ssh/authorized_keys",
                             "keyData": admin_key}]}}},
                 "networkProfile": {"networkInterfaces": [
                     {"id": nic_id, "properties": {"deleteOption": "Delete"}}]}}})
        if code >= 400:
            _raise_azure("virtual machine", vm_name, code, body)
        self._created.append(("vm", vm_name))
        log.info("regression: created %s for %s", vm_name, run_id)

    def _vm_state(self, vm_name: str) -> str:
        """Provisioning and agent state, for the log."""
        code, body = self._arm(
            "GET",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/instanceView")
        if code >= 400:
            return f"instanceView HTTP {code}"
        power = ",".join(s.get("code", "") for s in body.get("statuses", []) or [])
        agent = (body.get("vmAgent") or {}).get("statuses") or []
        return f"{power} agent={agent[0].get('displayStatus') if agent else 'none'}"

    def _wait_ready(self, vm_name: str, minutes: int = 8) -> bool:
        """Ready means the host answers the call the test is about to make.

        Reading readiness off the vmAgent status field cost a full 12-minute
        wait and a failed run: the field carries code
        'ProvisioningState/succeeded' and says 'Ready' only in displayStatus,
        so the condition was unsatisfiable while both hosts were up. Running
        the command settles it by observation, and the host is ready exactly
        when the thing that needs it works.

        Every attempt is logged. Silence for eight minutes is indistinguishable
        from a hung run, and a host the platform is slow to bring up should say
        so while it happens rather than only in the failure.
        """
        deadline = time.time() + minutes * 60
        started = time.time()
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            # 30s, not the 180s default. A runCommand against a host whose
            # agent is not up blocks rather than erroring, so the default
            # made each attempt cost three minutes and the whole wait ran
            # 180 * n seconds while reporting nothing.
            code, body = self._arm(
                "POST",
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
                {"commandId": "RunShellScript", "script": ["echo ready"]},
                timeout=30)
            if code < 400:
                for entry in body.get("value", []) or []:
                    if "ready" in (entry.get("message") or ""):
                        log.info("regression: %s answered after %ds (%d attempts)",
                                 vm_name, int(time.time() - started), attempt)
                        return True
            log.info("regression: waiting on %s %ds/%ds -- run command HTTP %s, %s",
                     vm_name, int(time.time() - started), minutes * 60, code,
                     self._vm_state(vm_name))
            time.sleep(5)
        log.info("regression: %s never answered in %d minutes -- %s",
                 vm_name, minutes, self._vm_state(vm_name))
        return False

    def _controller_pid(self, vm_name: str):
        """The controller pid the host wrote down, or None."""
        for line in self._host_logs.get(vm_name, []):
            if "spawned controller pid=" in line:
                tail = line.split("spawned controller pid=", 1)[1].strip()
                digits = ""
                for ch in tail:
                    if ch.isdigit():
                        digits += ch
                    else:
                        break
                if digits:
                    return int(digits)
        return None

    def _pid_alive(self, vm_name: str, pid) -> bool:
        """kill -0, the same question the components ask."""
        if pid is None:
            return False
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript",
             "script": [f"kill -0 {int(pid)} 2>/dev/null && echo RUNNING || echo GONE"]})
        if code >= 400:
            return False
        for entry in body.get("value", []) or []:
            if "RUNNING" in (entry.get("message") or ""):
                return True
        return False

    def _host_log(self, vm_name: str) -> list:
        """Read back what the host recorded about itself."""
        code, body = self._arm(
            "POST",
            f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
            {"commandId": "RunShellScript",
             "script": [f"cat {HOST_LOG} 2>/dev/null || echo 'NO HOST LOG'; "
                        f"echo '-- cloud-init --'; "
                        f"tail -n 15 /var/log/cloud-init-output.log 2>/dev/null"]})
        if code >= 400:
            return [f"could not read host log: HTTP {code}"]
        lines = []
        for entry in body.get("value", []) or []:
            lines.extend((entry.get("message") or "").splitlines())
        return [ln for ln in lines if ln.strip()]

    def _log_host(self, vm_name: str) -> None:
        """Put the host's own account of itself into the run's record."""
        lines = self._host_log(vm_name)
        self._host_logs[vm_name] = lines
        for line in lines:
            log.info("regression host %s | %s", vm_name, line)

    # -- Azure ---------------------------------------------------------------
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

    def _create_vm(self, vm_name: str, run_id: str,
                   controller_seconds: int = 7200) -> None:
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

        admin_user = os.environ.get("AZ_VM_ADMIN_USERNAME", "").strip()
        admin_key = os.environ.get("AZ_VM_ADMIN_SSH_PUBKEY", "").strip()
        if not admin_user or not admin_key:
            _raise_azure("virtual machine", vm_name, 0,
                         {"error": "AZ_VM_ADMIN_USERNAME or AZ_VM_ADMIN_SSH_PUBKEY "
                                   "is not set; a VM PUT with an empty key is "
                                   "rejected by Azure"})
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
                     "adminUsername": admin_user,
                     "customData": self._cloud_init(run_id, controller_seconds),
                     "linuxConfiguration": {
                         "disablePasswordAuthentication": True,
                         "ssh": {"publicKeys": [{
                             "path": f"/home/{admin_user}/.ssh/authorized_keys",
                             "keyData": admin_key}]}}},
                 "networkProfile": {"networkInterfaces": [
                     {"id": nic_id, "properties": {"deleteOption": "Delete"}}]}}})
        if code >= 400:
            _raise_azure("virtual machine", vm_name, code, body)
        self._created.append(("vm", vm_name))
        log.info("regression: created %s for %s", vm_name, run_id)

    def _wait_ready(self, vm_name: str, minutes: int = 8) -> bool:
        """Ready means the host answers the call the test is about to make.

        Reading readiness off the vmAgent status field cost a full 12-minute
        wait and a failed run: the field carries code
        'ProvisioningState/succeeded' and says 'Ready' only in displayStatus,
        so the condition was unsatisfiable while both hosts were up. Running
        the command settles it by observation, and the host is ready exactly
        when the thing that needs it works.
        """
        deadline = time.time() + minutes * 60
        while time.time() < deadline:
            code, body = self._arm(
                "POST",
                f"/providers/Microsoft.Compute/virtualMachines/{vm_name}/runCommand",
                {"commandId": "RunShellScript", "script": ["echo ready"]})
            if code < 400:
                for entry in body.get("value", []) or []:
                    if "ready" in (entry.get("message") or ""):
                        return True
            time.sleep(15)
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

    # -- bookkeeping ---------------------------------------------------------
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
                "host_logs": self._host_logs,
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
