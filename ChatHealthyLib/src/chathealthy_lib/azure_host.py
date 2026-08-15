# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Whether a pipeline run's host, and the process on it, are still alive.

A run records the VM it provisioned and the process id of the Controller on
it. Anything that must decide whether that run is still going asks this
service rather than inferring it from a clock: a lease that has not yet
expired says only that nobody has waited long enough, while a deallocated VM
or an absent process says the run is over now.

Stdlib only, so it inlines into an Azure Automation runbook unchanged.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_ARM = "https://management.azure.com"


class HostVerdict:
    """What was found, and whether it settles the question.

    `alive` and `dead` are answers. `unknown` is the absence of one, and its
    callers must not treat it as either -- an Azure that will not answer is
    not evidence that a run has stopped.
    """

    ALIVE = "alive"
    DEAD = "dead"
    UNKNOWN = "unknown"

    def __init__(self, state: str, detail: str) -> None:
        self.state = state
        self.detail = detail

    @property
    def is_dead(self) -> bool:
        return self.state == self.DEAD

    @property
    def is_alive(self) -> bool:
        return self.state == self.ALIVE

    @property
    def is_known(self) -> bool:
        return self.state != self.UNKNOWN

    def __repr__(self) -> str:
        return f"HostVerdict({self.state}, {self.detail!r})"


class ChatHealthyAzureHost:
    """Answers, for one subscription and resource group, whether a run's
    host and Controller process are alive.

    Authenticates as pipelineEditor through its client credentials. The
    managed-identity path is kept for hosts that carry one; the Automation
    Account does not, which is why every token fetch through IMDS failed
    silently and both the Watchdog and the reaper reaped nothing.
    """

    _DEAD_POWER_STATES = ("deallocated", "deallocating", "stopped", "stopping")

    def __init__(self, subscription_id: str | None = None,
                 resource_group: str | None = None) -> None:
        self._subscription = (subscription_id
                              or os.environ.get("AZ_SUBSCRIPTION_ID", "")).strip()
        self._resource_group = (resource_group
                                or os.environ.get("AZ_VM_RESOURCE_GROUP", "")
                                or os.environ.get("AZ_RESOURCE_GROUP", "")).strip()
        self._log = ChatHealthyLoggingService()
        self._token: str | None = None

    def verdict(self, vm_name: str, pid: int | None = None) -> HostVerdict:
        """Is the run on this host still going?

        A host that is gone or powered down is a dead run. A host that is
        running answers only for itself unless a pid was recorded, in which
        case the process settles it.
        """
        if not vm_name:
            return HostVerdict(HostVerdict.UNKNOWN, "no vm_name recorded")
        if not (self._subscription and self._resource_group):
            return HostVerdict(HostVerdict.UNKNOWN,
                               "subscription or resource group not configured")
        power = self._power_state(vm_name)
        if power == "absent":
            return HostVerdict(HostVerdict.DEAD, f"{vm_name} no longer exists")
        if power.startswith("unknown"):
            return HostVerdict(HostVerdict.UNKNOWN, f"{vm_name}: {power}")
        if power in self._DEAD_POWER_STATES:
            return HostVerdict(HostVerdict.DEAD, f"{vm_name} is {power}")
        if pid is None:
            return HostVerdict(HostVerdict.ALIVE,
                               f"{vm_name} is {power}; no pid recorded")
        return self._process_verdict(vm_name, power, pid)

    def _process_verdict(self, vm_name: str, power: str, pid: int) -> HostVerdict:
        running = self._pid_running(vm_name, pid)
        if running is None:
            return HostVerdict(HostVerdict.UNKNOWN,
                               f"{vm_name} is {power}; could not read pid {pid}")
        if running:
            return HostVerdict(HostVerdict.ALIVE, f"pid {pid} is running on {vm_name}")
        return HostVerdict(HostVerdict.DEAD, f"pid {pid} is gone on {vm_name}")

    def _power_state(self, vm_name: str) -> str:
        url = (f"{_ARM}/subscriptions/{self._subscription}/resourceGroups/"
               f"{self._resource_group}/providers/Microsoft.Compute/"
               f"virtualMachines/{vm_name}/instanceView?api-version=2024-03-01")
        try:
            body = self._get(url)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                return "absent"
            return f"unknown_http_{exc.code}"
        except Exception as exc:  # noqa: BLE001
            return f"unknown_{type(exc).__name__}"
        for status in body.get("statuses", []) or []:
            code = status.get("code", "")
            if code.startswith("PowerState/"):
                return code.split("/", 1)[1]
        return "unknown_no_power_state"

    def _pid_running(self, vm_name: str, pid: int) -> bool | None:
        """Ask the host itself. None means the host would not answer."""
        url = (f"{_ARM}/subscriptions/{self._subscription}/resourceGroups/"
               f"{self._resource_group}/providers/Microsoft.Compute/"
               f"virtualMachines/{vm_name}/runCommand?api-version=2024-03-01")
        payload = {
            "commandId": "RunShellScript",
            "script": [f"kill -0 {int(pid)} 2>/dev/null && echo RUNNING || echo GONE"],
        }
        try:
            body = self._post(url, payload)
        except Exception as exc:  # noqa: BLE001
            self._log.warning("azure host: run command on %s failed (%s)",
                              vm_name, f"{type(exc).__name__}: {str(exc)[:160]}")
            return None
        rendered = json.dumps(body)
        if "RUNNING" in rendered:
            return True
        if "GONE" in rendered:
            return False
        return None

    def _bearer(self) -> str:
        if self._token:
            return self._token
        tenant = os.environ.get("PIPELINEEDITOR_AZURE_TENANT_ID", "").strip()
        app_id = os.environ.get("PIPELINEEDITOR_AZURE_CLIENT_ID", "").strip()
        secret = os.environ.get("PIPELINEEDITOR_AZURE_CLIENT_SECRET", "").strip()
        if tenant and app_id and secret:
            self._token = self._client_credentials_token(tenant, app_id, secret)
            return self._token
        self._token = self._managed_identity_token()
        return self._token

    @staticmethod
    def _client_credentials_token(tenant: str, app_id: str, secret: str) -> str:
        body = urllib.parse.urlencode({
            "grant_type": "client_credentials",
            "client_id": app_id,
            "client_secret": secret,
            "scope": f"{_ARM}/.default",
        }).encode("utf-8")
        req = urllib.request.Request(
            f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8"))["access_token"]

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
                component="ChatHealthyAzureHost")
        return token

    def _get(self, url: str) -> dict:
        req = urllib.request.Request(
            url, headers={"Authorization": f"Bearer {self._bearer()}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _post(self, url: str, payload: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode("utf-8"), method="POST",
            headers={"Authorization": f"Bearer {self._bearer()}",
                     "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}
