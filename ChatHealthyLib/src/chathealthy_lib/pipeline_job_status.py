# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Whether a pipeline job is still running, has failed, or cannot be judged.

One question, asked in one place. Every component that acts on a stopped job
asks it here rather than assembling its own answer: the Watchdog deletes the
host, the ReservationReaper corrects the job metadata, and both act on the
same verdict rather than two reconstructions of it.

The judgment is an observation, not a deadline. A reservation that has not yet
expired says only that nobody has waited long enough; a deallocated host, or an
absent Controller process on a running one, says the job has stopped now.

Stdlib only, so it inlines into an Azure Automation runbook unchanged.
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_ARM = "https://management.azure.com"
_LIFECYCLE = "cluster_lifecycle"
_RUNS = "pipeline.runs"


class JobVerdict:
    """RUNNING, STOPPED, or UNKNOWN.

    STOPPED means the job is no longer running, which covers both a run that
    finished and one that died -- the detail says which. The distinction the
    callers need is whether anything is still working, not how it ended: a
    lock held by a run that succeeded is as stale as one held by a run that
    crashed.

    UNKNOWN is the absence of an answer and must not be read as either. An
    Azure that will not answer is no evidence a job has stopped, and a job
    that names no host cannot be judged at all.
    """

    RUNNING = "running"
    STOPPED = "stopped"
    UNKNOWN = "unknown"

    def __init__(self, state: str, detail: str) -> None:
        self.state = state
        self.detail = detail

    @property
    def has_stopped(self) -> bool:
        return self.state == self.STOPPED

    @property
    def is_running(self) -> bool:
        return self.state == self.RUNNING

    @property
    def is_known(self) -> bool:
        return self.state != self.UNKNOWN

    def __repr__(self) -> str:
        return f"JobVerdict({self.state}, {self.detail!r})"


# A host serves one run command at a time, and runCommand draws on the
# Update VM throttle bucket: twelve burst, four a minute, per VM.
_RUN_COMMAND_TRIES = 8
_RUN_COMMAND_BUSY_WAIT_SECONDS = 10
# Fifteen minutes: the interval between scheduled ticks. A tick that polls the
# full bound does overlap its successor, since it spends time getting there --
# but only just, and only two can ever be in flight, which needs the second to
# also run long. Azure's own limit is ninety minutes, which would hang a
# scheduled reaper for an hour and a half. Observed completions are 10 to 20
# seconds, so this bound is reached only by something genuinely wrong -- and it
# says so rather than returning an unknown that leaves a dead run alone.
_RUN_COMMAND_MAX_MINUTES = 15


class ChatHealthyPipelineJobStatus:
    """Answers whether a pipeline job has failed.

    Reads the job's own records to find the host and process it claimed, then
    asks Azure what is true of them now. Authenticates as pipelineEditor via
    its client credentials; the managed-identity path remains for hosts that
    carry one, which the Automation Account does not.
    """

    _STOPPED_POWER_STATES = ("deallocated", "deallocating", "stopped", "stopping")
    _TERMINAL_RUN_STATUSES = frozenset(
        {"succeeded", "failed", "aborted", "completed"})

    def __init__(self, mongo_client, admin_db: str = "pipelineAdmin",
                 subscription_id: str | None = None,
                 resource_group: str | None = None) -> None:
        self._db = mongo_client[admin_db]
        # AUTOMATION_* are what the runbooks resolve, and they are the values
        # that are correct. AZ_VM_RESOURCE_GROUP and AZ_RESOURCE_GROUP name
        # groups that do not exist -- reaching for those made every VM lookup
        # 404, which this class would have read as "the host is gone".
        self._subscription = (subscription_id
                              or os.environ.get("AUTOMATION_SUBSCRIPTION_ID", "")
                              or os.environ.get("AZ_SUBSCRIPTION_ID", "")).strip()
        self._resource_group = (resource_group
                                or os.environ.get("AUTOMATION_RESOURCE_GROUP", "")
                                or os.environ.get("AZ_VM_RESOURCE_GROUP", "")).strip()
        self._log = ChatHealthyLoggingService()
        self._token: str | None = None

    # ── the question ────────────────────────────────────────────────────────
    def verdict(self, run_id: str) -> JobVerdict:
        if not run_id:
            return JobVerdict(JobVerdict.UNKNOWN, "no run_id")
        run = self._db[_RUNS].find_one({"run_id": run_id}) or {}
        status = str(run.get("status", "")).lower()
        if status in self._TERMINAL_RUN_STATUSES:
            return JobVerdict(JobVerdict.STOPPED,
                              f"run already recorded {status}")
        reservation = self._db[_LIFECYCLE].find_one({"_id": run_id}) or {}
        vm_name = reservation.get("vm_name") or run.get("vm_name")
        pid = reservation.get("controller_pid") or run.get("controller_pid")
        if not vm_name:
            return JobVerdict(JobVerdict.UNKNOWN,
                              f"{run_id} names no host to ask about")
        if not (self._subscription and self._resource_group):
            return JobVerdict(JobVerdict.UNKNOWN,
                              "subscription or resource group not configured")
        return self._host_verdict(run_id, vm_name, pid)

    def failed_jobs(self) -> list[dict]:
        """Every job that has failed, with the host and pids to clean up.

        The Watchdog asks for this rather than deciding per VM: a job that is
        recorded as going and whose Controller is not running is over, whether
        or not it left a lifecycle row behind. Each entry names the host, the
        Controller pid, and the pids of the workers it spawned, so the caller
        kills what the job left rather than only the machine.

        Jobs whose state cannot be established are absent -- an unknown is not
        a failure, and killing on one would end live work.
        """
        failed = []
        for run in self._db[_RUNS].find(
                {"status": {"$nin": list(self._TERMINAL_RUN_STATUSES)}},
                {"run_id": 1, "vm_name": 1, "controller_pid": 1}):
            run_id = run.get("run_id")
            verdict = self.verdict(run_id)
            if not verdict.has_stopped:
                continue
            reservation = self._db[_LIFECYCLE].find_one({"_id": run_id}) or {}
            controller_pid = (reservation.get("controller_pid")
                              or run.get("controller_pid"))
            worker_pids = self._worker_pids(run_id)
            vm_name = reservation.get("vm_name") or run.get("vm_name")
            # The host decides what is still running. The recorded pids are
            # what the run intended to start and are carried for context, not
            # as the kill list -- a process that outlived its bookkeeping is
            # exactly the one that is not in them.
            observed = self.killable_processes(vm_name) if vm_name else None
            kill = [pr["pid"] for pr in (observed or [])]
            failed.append({
                "run_id": run_id,
                "vm_name": vm_name,
                "controller_pid": controller_pid,
                "worker_pids": worker_pids,
                # Controller first: it spawns workers, so killing it before
                # them stops it dispatching more while the list is worked.
                "pids_to_kill": kill,
                "host_answered": observed is not None,
                "reason": verdict.detail,
            })
        return failed

    # /proc only. pgrep and ps are absent from the pipeline image, which is
    # why control_runner's own pgrep -P kill path has never run; everything
    # here uses tr, sed and awk, which the image does have.
    #
    # Each process names its own run: a worker carries --run-id in argv, the
    # Controller carries RUN_ID in its environment. Both are read from /proc,
    # so a pid is tied to a run rather than matched on a program name.
    _PROCESS_SCAN = r"""
self=$$
for d in /proc/[0-9]*; do
  pid=${d##*/}
  [ "$pid" = "$self" ] && continue
  ppid=$(awk '{print $4}' "$d/stat" 2>/dev/null)
  [ "$ppid" = "$self" ] && continue
  st=$(awk '{print $3}' "$d/stat" 2>/dev/null)
  args=$(tr '\000' ' ' < "$d/cmdline" 2>/dev/null)
  if [ -z "$args" ]; then
    # Defunct: the command line is gone, but stat still names the program.
    comm=$(awk '{print $2}' "$d/stat" 2>/dev/null)
    case "$comm" in
      *control_runner*|*pipeline_worker*|*provider_worker*|*python*) ;;
      *) continue ;;
    esac
    [ "$st" = "Z" ] || continue
    echo "$pid|UNKNOWN|$st|$comm"
    continue
  fi
  case "$args" in
    *control_runner*|*pipeline_worker*|*provider_worker*) ;;
    *) continue ;;
  esac
  rid=$(printf %s "$args" | sed -n 's/.*--run-id \([^ ]*\).*/\1/p')
  if [ -z "$rid" ]; then
    rid=$(tr '\000' '\n' < "$d/environ" 2>/dev/null | sed -n 's/^RUN_ID=//p' | head -1)
  fi
  echo "$pid|${rid:-UNKNOWN}|${st:-?}|$args"
done
"""

    def host_processes(self, vm_name: str) -> list[dict] | None:
        """The pipeline processes running on a host, each tied to its run.

        This is the only honest source for a zombie. What a run recorded
        starting is what it intended; a process that outlived its bookkeeping,
        or one a worker spawned itself, appears here and nowhere else.

        Returns None when the host will not answer -- distinct from an empty
        list, which is the host answering that nothing is running.
        """
        raw = self._run_shell(vm_name, self._PROCESS_SCAN)
        if raw is None:
            return None
        found = []
        for line in raw.splitlines():
            parts = line.strip().split("|", 3)
            if len(parts) != 4 or not parts[0].isdigit():
                continue
            pid, run_id, state, args = parts
            found.append({
                "pid": int(pid),
                "run_id": None if run_id == "UNKNOWN" else run_id,
                "state": state,
                "command": args[:300],
                # A Unix zombie proper: exited, still in the table because
                # nothing reaped it. Killing it does nothing; its parent is
                # the problem.
                "defunct": state == "Z",
            })
        return found

    def killable_processes(self, vm_name: str) -> list[dict] | None:
        """Processes on the host whose own run has failed.

        Every pid is mapped to its run and that run judged before it is
        offered for killing. A process whose run is still going is left alone,
        and so is one that cannot name its run -- an unidentified process is
        not evidence of a dead job, and this host may be carrying live work.
        """
        processes = self.host_processes(vm_name)
        if processes is None:
            return None
        killable, judged = [], {}
        for proc in processes:
            if proc["defunct"] or not proc["run_id"]:
                continue
            run_id = proc["run_id"]
            if run_id not in judged:
                judged[run_id] = self.verdict(run_id)
            if judged[run_id].has_stopped:
                killable.append({**proc, "reason": judged[run_id].detail})
        return killable

    def _run_shell(self, vm_name: str, script: str) -> str | None:
        url = (f"{_ARM}/subscriptions/{self._subscription}/resourceGroups/"
               f"{self._resource_group}/providers/Microsoft.Compute/"
               f"virtualMachines/{vm_name}/runCommand?api-version=2024-03-01")
        try:
            body = self._post(url, {"commandId": "RunShellScript",
                                    "script": [script]})
        except Exception as exc:  # noqa: BLE001
            self._log.warning("job status: run command on %s failed (%s)",
                              vm_name, f"{type(exc).__name__}: {str(exc)[:160]}")
            return None
        for entry in (body.get("value") or []):
            message = entry.get("message") or ""
            if "StdOut" in message or message.strip():
                return message
        return ""

    def _worker_pids(self, run_id: str) -> list[int]:
        """Pids the run's workers recorded when they claimed their work.

        A worker stamps its own pid on the item as claimed_by, so the items
        are the register of which processes the run started. Items already
        terminal are included: a zombie is precisely a process that outlived
        the record of its work being finished.
        """
        pids = []
        for item in self._db["pipeline.work_items"].find(
                {"run_id": run_id, "claimed_by": {"$exists": True}},
                {"claimed_by": 1}):
            pid = item.get("claimed_by")
            if isinstance(pid, int) and pid not in pids:
                pids.append(pid)
        return pids

    # ── how it is answered ──────────────────────────────────────────────────
    def _host_verdict(self, run_id: str, vm_name: str, pid) -> JobVerdict:
        power = self._power_state(vm_name)
        if power == "absent":
            return JobVerdict(JobVerdict.STOPPED, f"{vm_name} no longer exists")
        if power.startswith("unknown"):
            return JobVerdict(JobVerdict.UNKNOWN, f"{vm_name}: {power}")
        if power in self._STOPPED_POWER_STATES:
            return JobVerdict(JobVerdict.STOPPED, f"{vm_name} is {power}")
        if pid is None:
            return JobVerdict(JobVerdict.RUNNING,
                              f"{vm_name} is {power}; no pid recorded")
        running = self._pid_running(vm_name, int(pid))
        if running is None:
            return JobVerdict(JobVerdict.UNKNOWN,
                              f"{vm_name} is {power}; could not read pid {pid}")
        if running:
            return JobVerdict(JobVerdict.RUNNING,
                              f"pid {pid} is running on {vm_name}")
        return JobVerdict(JobVerdict.STOPPED, f"pid {pid} is gone on {vm_name}")

    def _power_state(self, vm_name: str) -> str:
        url = (f"{_ARM}/subscriptions/{self._subscription}/resourceGroups/"
               f"{self._resource_group}/providers/Microsoft.Compute/"
               f"virtualMachines/{vm_name}/instanceView?api-version=2024-03-01")
        try:
            body = self._get(url)
        except urllib.error.HTTPError as exc:
            # A 404 is only evidence the VM is gone when Azure says the VM is
            # what it could not find. A missing or misnamed resource group
            # also answers 404, and reading that as "absent" would judge every
            # job stopped -- stamping live runs failed and deleting their
            # hosts. AZ_VM_RESOURCE_GROUP in .env names a group that does not
            # exist, so this is not hypothetical.
            if exc.code == 404:
                detail = ""
                try:
                    detail = exc.read().decode("utf-8", "replace")[:400]
                except Exception:  # noqa: BLE001
                    pass
                if "ResourceNotFound" in detail and "virtualMachines" in detail:
                    return "absent"
                return f"unknown_http_404:{detail[:120]}"
            return f"unknown_http_{exc.code}"
        except Exception as exc:  # noqa: BLE001
            return f"unknown_{type(exc).__name__}"
        for status in body.get("statuses", []) or []:
            code = status.get("code", "")
            if code.startswith("PowerState/"):
                return code.split("/", 1)[1]
        return "unknown_no_power_state"

    def _pid_running(self, vm_name: str, pid: int) -> bool | None:
        url = (f"{_ARM}/subscriptions/{self._subscription}/resourceGroups/"
               f"{self._resource_group}/providers/Microsoft.Compute/"
               f"virtualMachines/{vm_name}/runCommand?api-version=2024-03-01")
        payload = {"commandId": "RunShellScript",
                   "script": [f"kill -0 {pid} 2>/dev/null && echo RUNNING || echo GONE"]}
        try:
            rendered = json.dumps(self._post(url, payload))
        except Exception as exc:  # noqa: BLE001
            self._log.warning("job status: run command on %s failed (%s)",
                              vm_name, f"{type(exc).__name__}: {str(exc)[:160]}")
            return None
        if "RUNNING" in rendered:
            return True
        if "GONE" in rendered:
            return False
        return None

    # ── credentials and transport ───────────────────────────────────────────
    def _bearer(self) -> str:
        if self._token:
            return self._token
        tenant = os.environ.get("PIPELINEEDITOR_AZURE_TENANT_ID", "").strip()
        app_id = os.environ.get("PIPELINEEDITOR_AZURE_CLIENT_ID", "").strip()
        secret = os.environ.get("PIPELINEEDITOR_AZURE_CLIENT_SECRET", "").strip()
        if tenant and app_id and secret:
            body = urllib.parse.urlencode({
                "grant_type": "client_credentials", "client_id": app_id,
                "client_secret": secret, "scope": f"{_ARM}/.default"}).encode("utf-8")
            req = urllib.request.Request(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data=body,
                headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._token = json.loads(resp.read().decode("utf-8"))["access_token"]
                return self._token
        self._token = self._managed_identity_token()
        return self._token

    @staticmethod
    def _managed_identity_token() -> str:
        endpoint = os.environ.get("IDENTITY_ENDPOINT")
        header = os.environ.get("IDENTITY_HEADER")
        if endpoint and header:
            req = urllib.request.Request(
                f"{endpoint}?resource={_ARM}/&api-version=2019-08-01",
                headers={"X-IDENTITY-HEADER": header})
        else:
            req = urllib.request.Request(
                "http://169.254.169.254/metadata/identity/oauth2/token"
                f"?api-version=2018-02-01&resource={_ARM}/",
                headers={"Metadata": "true"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            token = json.loads(resp.read().decode("utf-8")).get("access_token")
        if not token:
            raise ChatHealthyException(
                mode="auth_error",
                message="no ARM token: neither pipelineEditor client credentials "
                        "nor a managed identity answered",
                component="ChatHealthyPipelineJobStatus")
        return token

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._bearer()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, url: str, payload: dict) -> dict:
        """POST, and follow an accepted operation to its result.

        runCommand is asynchronous: it answers 202 with an empty body and the
        result is reached by polling the Azure-AsyncOperation URL from the
        response headers. Returning the 202 body meant every run command this
        service ever made came back empty, so _pid_running found neither
        RUNNING nor GONE and answered None -- every verdict was UNKNOWN, no
        controller was ever observed to have died, and the reaper and Watchdog
        never acted on one. Reading the host was inert from the day it was
        written.
        """
        # A host serves one run command at a time -- documented in the Run
        # Command restrictions -- and runCommand draws on the Update VM
        # throttle bucket, twelve burst and four a minute per VM. The reaper
        # and the Watchdog both ask hosts questions, so they collide with each
        # other and with anything else reading that host. Raising on a busy
        # slot made the caller answer None, an unknown verdict leaves the run
        # alone, and two components doing their job at once stopped either
        # from doing it.
        for attempt in range(_RUN_COMMAND_TRIES):
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"), method="POST",
                headers={"Authorization": f"Bearer {self._bearer()}",
                         "Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    raw = resp.read().decode("utf-8")
                    status = resp.status
                    headers = dict(resp.headers)
                break
            except urllib.error.HTTPError as exc:
                if exc.code not in (409, 429) or attempt == _RUN_COMMAND_TRIES - 1:
                    raise
                wait = _RUN_COMMAND_BUSY_WAIT_SECONDS
                if exc.code == 429:
                    try:
                        wait = max(wait, int((exc.headers or {}).get("Retry-After", 0)))
                    except (TypeError, ValueError):
                        pass
                self._log.info(
                    "job status: run command HTTP %d, waiting %ds (attempt %d of %d)",
                    exc.code, wait, attempt + 1, _RUN_COMMAND_TRIES)
                time.sleep(wait)
        if status != 202:
            return json.loads(raw) if raw else {}
        async_url = (headers.get("Azure-AsyncOperation")
                     or headers.get("Location"))
        if not async_url:
            self._log.warning("job status: 202 with no async operation url")
            return {}
        return self._await_async(async_url)

    def _await_async(self, url: str, minutes: int = _RUN_COMMAND_MAX_MINUTES) -> dict:
        """Poll an accepted operation, by whichever protocol Azure gave us.

        Two exist, and this API documents the one that is easy to miss.
        Azure-AsyncOperation returns an envelope carrying a status field.
        Location with monitor=true -- the only header the Run Command
        reference documents -- answers 202 while the operation runs and 200
        with the result body itself when it finishes, carrying no status field
        at all. Recognising only the envelope means a Location response is
        never seen as finished: the caller polls to its ceiling and returns
        nothing, which reads exactly like a host that cannot be reached.
        """
        deadline = time.time() + minutes * 60
        started = time.time()
        while time.time() < deadline:
            code, body = self._get_with_status(url)
            state = str(body.get("status") or "").lower()
            waited = int(time.time() - started)
            if code >= 400:
                self._log.warning("job status: polling operation returned %s", code)
                return {}
            if state in ("succeeded", "failed", "canceled"):
                self._log.info("job status: run command %s after %ds",
                               state, waited)
                if state != "succeeded":
                    return {}
                return (body.get("properties") or {}).get("output") or body
            if code == 200 and not state:
                self._log.info("job status: run command complete after %ds", waited)
                return body
            self._log.info("job status: run command %s %ds/%ds",
                           state or f"HTTP {code}", waited, minutes * 60)
            # Ten seconds. Operation polling draws on its own throttle bucket
            # -- fifteen a minute refill, forty-five burst, per resource -- and
            # the reaper and the Watchdog poll their own operations at the same
            # time. A five-second poll is twelve a minute from one caller alone.
            time.sleep(10)
        # Azure's own maximum for a run command, not a number chosen here.
        # A ceiling shorter than the operation invents a verdict: the caller
        # gets nothing, reads it as unknown, and leaves a dead run alone --
        # which is the failure this whole path exists to prevent.
        self._log.warning("job status: run command did not finish in %d minutes",
                          minutes)
        return {}

    def _get_with_status(self, url: str) -> tuple:
        """A GET that reports its status code, for the 202-then-200 protocol."""
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._bearer()}"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read().decode("utf-8")
                return resp.status, (json.loads(raw) if raw else {})
        except urllib.error.HTTPError as exc:
            try:
                return exc.code, json.loads(exc.read().decode("utf-8"))
            except Exception:  # noqa: BLE001
                return exc.code, {}
        except Exception as exc:  # noqa: BLE001
            self._log.warning("job status: polling operation failed (%s)",
                              f"{type(exc).__name__}: {str(exc)[:120]}")
            return 0, {}
