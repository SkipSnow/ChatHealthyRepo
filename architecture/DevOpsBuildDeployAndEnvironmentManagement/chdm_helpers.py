"""ChatHealthyDataMigrator deploy-side helpers.

Owns the check-or-configure logic for the persistent infrastructure the
migrator chain depends on:

  - Undelegated VM subnet inside ChatHealthy-VNet.
  - Hybrid Worker Group on the ChatHealthyJobManager Automation Account.
  - Role assignments on the AA's system-assigned managed identity
    (Virtual Machine Contributor + Automation Operator on the VM RG).
  - Orchestrator runbook webhook (URL captured at create time, persisted as
    an Automation Variable so subsequent deploys reuse it; written into the
    gateway Function App settings so the gateway forwards to it).

Each function fails loud. No soft fallbacks; the operator sees the real
az error when something is wrong.
"""
from __future__ import annotations

import base64
import datetime
import json
import pathlib
import subprocess
import sys


_AUTOMATION_API = "2023-11-01"
_NETWORK_API = "2024-01-01"
_VM_SUBNET_NAME = "hybrid-worker-subnet"
_VM_SUBNET_PREFIX = "10.0.2.0/24"
_VNET_NAME = "ChatHealthy-VNet"
_HYBRID_WORKER_GROUP = "ChatHealthyDataMigratorWorkGroup"
_ORCHESTRATOR_WEBHOOK_NAME = "ChatHealthyDataMigratorOrchestratorWebhook"
_ORCHESTRATOR_WEBHOOK_VAR = "ORCHESTRATOR_WEBHOOK_URL_persisted"


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _step(msg: str) -> None:
    print(f"[chdm] {msg}", flush=True)


def _az(args: list[str], *, capture_json: bool = False) -> dict | str:
    """Run an `az` command. Returns stdout (stripped) on success.

    capture_json=True parses stdout as JSON and returns the dict. Caller
    is responsible for picking the right `-o` flag in args.
    """
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az command failed (exit {r.returncode})\n"
            f"  cmd: {' '.join(args)}\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )
    out = (r.stdout or "").strip()
    if capture_json:
        return json.loads(out) if out else {}
    return out


def _az_try(args: list[str]) -> tuple[int, str, str]:
    """Run an `az` command tolerating a non-zero exit. Returns
    (returncode, stdout, stderr). Used by ensure-style checks that need
    to distinguish 'absent' from 'broken'."""
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _subscription_id() -> str:
    return str(_az(["az", "account", "show", "--query", "id", "-o", "tsv"]))


def chdm_ensure_vm_subnet(vnet_rg: str) -> str:
    """Ensure the undelegated VM subnet exists inside ChatHealthy-VNet.

    Returns the subnet ARM resource ID — caller wires it through to the
    provisioner as the AZ_VM_SUBNET_ID Automation Variable.
    """
    _step(
        f"verifying VM subnet '{_VM_SUBNET_NAME}' in vnet '{_VNET_NAME}' "
        f"(rg={vnet_rg})"
    )
    rc, out, _ = _az_try([
        "az", "network", "vnet", "subnet", "show",
        "--name", _VM_SUBNET_NAME,
        "--vnet-name", _VNET_NAME,
        "--resource-group", vnet_rg,
        "--query", "id", "-o", "tsv",
    ])
    if rc == 0 and out:
        _step(f"  subnet '{_VM_SUBNET_NAME}' exists — no-op")
        return out
    _step(f"  subnet '{_VM_SUBNET_NAME}' missing — creating ({_VM_SUBNET_PREFIX})")
    subnet_id = str(_az([
        "az", "network", "vnet", "subnet", "create",
        "--name", _VM_SUBNET_NAME,
        "--vnet-name", _VNET_NAME,
        "--resource-group", vnet_rg,
        "--address-prefixes", _VM_SUBNET_PREFIX,
        "--query", "id", "-o", "tsv",
    ]))
    _step(f"  subnet '{_VM_SUBNET_NAME}' created.")
    return subnet_id


def chdm_ensure_hybrid_worker_group(aa_rg: str, aa: str) -> None:
    """Ensure the persistent Hybrid Worker Group exists on the Automation
    Account. Extension-based group type — the provisioner uses the
    extension-based onboarding flow."""
    _step(
        f"verifying hybrid worker group '{_HYBRID_WORKER_GROUP}' on "
        f"automation account '{aa}' (rg={aa_rg})"
    )
    sub = _subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{aa_rg}/providers/Microsoft.Automation"
        f"/automationAccounts/{aa}/hybridRunbookWorkerGroups/{_HYBRID_WORKER_GROUP}"
        f"?api-version={_AUTOMATION_API}"
    )
    rc, _, _ = _az_try([
        "az", "rest", "--method", "get", "--url", url, "-o", "none",
    ])
    if rc == 0:
        _step(f"  hybrid worker group '{_HYBRID_WORKER_GROUP}' exists — no-op")
        return
    _step(f"  hybrid worker group '{_HYBRID_WORKER_GROUP}' missing — creating")
    body = json.dumps({"properties": {}})
    _az([
        "az", "rest", "--method", "put", "--url", url,
        "--headers", "Content-Type=application/json",
        "--body", body, "-o", "none",
    ])
    _step(f"  hybrid worker group '{_HYBRID_WORKER_GROUP}' created.")


def chdm_ensure_aa_managed_identity(aa_rg: str, aa: str) -> str:
    """Ensure the Automation Account has a system-assigned managed identity.

    Returns the MI's principal id. If the AA doesn't have system-assigned
    identity enabled, this enables it. The MI is what the AA uses to start
    runbook jobs as itself, to read its own Automation Variables across
    jobs, and to perform the Azure operations the role-assignments below
    grant it.
    """
    _step(f"verifying system-assigned managed identity on '{aa}' (rg={aa_rg})")
    show = _az([
        "az", "automation", "account", "show",
        "--name", aa, "--resource-group", aa_rg,
        "--query", "identity.principalId", "-o", "tsv",
    ])
    pid = str(show)
    if pid and pid != "None":
        _step(f"  managed identity present — principalId={pid[:8]}...")
        return pid
    _step("  managed identity missing — enabling system-assigned identity")
    _az([
        "az", "automation", "account", "update",
        "--name", aa, "--resource-group", aa_rg,
        "--assign-identity", "[system]",
        "-o", "none",
    ])
    show = _az([
        "az", "automation", "account", "show",
        "--name", aa, "--resource-group", aa_rg,
        "--query", "identity.principalId", "-o", "tsv",
    ])
    pid = str(show)
    if not pid or pid == "None":
        sys.exit(
            f"ERROR: enabled system-assigned identity on {aa!r} but "
            f"identity.principalId is still empty in the follow-up read. "
            f"Inspect the AA in the portal."
        )
    _step(f"  managed identity enabled — principalId={pid[:8]}...")
    return pid


def chdm_ensure_role_assignment(principal_id: str, scope: str, role_name: str) -> None:
    """Ensure the given principal has the given role on the given scope.

    az role assignment create is NOT idempotent — it returns 409 if the
    assignment already exists. Check via list first; only create when
    missing.
    """
    _step(f"verifying role '{role_name}' for principal on scope (suffix={scope[-60:]})")
    raw = _az([
        "az", "role", "assignment", "list",
        "--assignee", principal_id,
        "--scope", scope,
        "--role", role_name,
        "-o", "json",
    ])
    try:
        existing = json.loads(raw or "[]")
    except Exception:
        existing = []
    if existing:
        _step(f"  role '{role_name}' already assigned — no-op")
        return
    _step(f"  role '{role_name}' missing — creating assignment")
    _az([
        "az", "role", "assignment", "create",
        "--assignee", principal_id,
        "--scope", scope,
        "--role", role_name,
        "-o", "none",
    ])
    _step(f"  role '{role_name}' assigned.")


def chdm_ensure_chdm_persistent_infrastructure(
    *,
    vm_rg: str,
    aa_rg: str,
    aa: str,
) -> str:
    """Whole-of-infrastructure ensure. Called once per deploy session at
    the start of the first azure_automation_runbook deploy for a CHDM
    runbook. Returns the VM subnet ARM resource id."""
    _step("=== verifying CHDM persistent infrastructure ===")
    subnet_id = chdm_ensure_vm_subnet(vm_rg)
    chdm_ensure_hybrid_worker_group(aa_rg, aa)

    sub = _subscription_id()
    aa_mi_pid = chdm_ensure_aa_managed_identity(aa_rg, aa)
    vm_rg_scope = f"/subscriptions/{sub}/resourceGroups/{vm_rg}"
    for role in ("Virtual Machine Contributor", "Network Contributor"):
        chdm_ensure_role_assignment(aa_mi_pid, vm_rg_scope, role)

    aa_scope = (
        f"/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
    )
    # The AA's own MI needs to PUT hybridRunbookWorkers on the AA (the
    # provisioner does this during the extension-based onboarding) and GET
    # the AA's own properties.automationHybridServiceUrl. Automation
    # Operator grants only job start/read; Contributor on the AA grants
    # write on child resources (per Microsoft Learn extension-based
    # Hybrid Worker install walkthrough).
    chdm_ensure_role_assignment(aa_mi_pid, aa_scope, "Contributor")
    return subnet_id


def _read_aa_variable(aa_rg: str, aa: str, name: str) -> str | None:
    """Return the decoded value of an Automation Variable, or None if absent.

    Values are stored JSON-encoded on the wire (the deploy handler that
    writes them does `json.dumps(value)`); we round-trip that.
    """
    sub = _subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}/variables/{name}"
        f"?api-version={_AUTOMATION_API}"
    )
    rc, out, _ = _az_try([
        "az", "rest", "--method", "get", "--url", url, "-o", "json",
    ])
    if rc != 0 or not out:
        return None
    try:
        body = json.loads(out)
        raw = body.get("properties", {}).get("value")
        if raw is None:
            return None
        # Variable values are stored as JSON-encoded strings; decode once.
        return json.loads(raw) if isinstance(raw, str) and raw.startswith('"') else str(raw)
    except Exception:
        return None


def _write_aa_variable(aa_rg: str, aa: str, name: str, value: str) -> None:
    sub = _subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}/variables/{name}"
        f"?api-version={_AUTOMATION_API}"
    )
    body = json.dumps({
        "name": name,
        "properties": {"value": json.dumps(value), "isEncrypted": True},
    })
    _az([
        "az", "rest", "--method", "put", "--url", url,
        "--headers", "Content-Type=application/json",
        "--body", body, "-o", "none",
    ])


def chdm_ensure_orchestrator_webhook(
    *,
    aa_rg: str,
    aa: str,
    runbook_name: str,
    expiry_years: int = 10,
) -> str:
    """Return a usable orchestrator webhook URL.

    Webhook URIs are only returned by Azure at create time, so we
    persist the URL as an Automation Variable on the AA and reuse it
    on subsequent deploys. If the persisted URL is missing OR the
    webhook resource on the AA is gone / expired, we delete-then-
    recreate via `az automation webhook create`, which mints the URI
    cryptographically and returns it. Long-lived expiry (default 10
    years) so re-minting almost never fires.
    """
    persisted = _read_aa_variable(aa_rg, aa, _ORCHESTRATOR_WEBHOOK_VAR)

    show_rc, show_out, _ = _az_try([
        "az", "automation", "webhook", "show",
        "--resource-group", aa_rg,
        "--automation-account-name", aa,
        "--name", _ORCHESTRATOR_WEBHOOK_NAME,
        "-o", "json",
    ])
    is_healthy = False
    if show_rc == 0 and show_out and persisted:
        try:
            body = json.loads(show_out)
            # CLI sometimes flattens fields to top level, sometimes nests
            # under properties — accept either.
            expiry = body.get("expiryTime") or body.get("properties", {}).get("expiryTime", "")
            enabled = bool(body.get("isEnabled") or body.get("properties", {}).get("isEnabled"))
            if enabled and expiry:
                exp_dt = datetime.datetime.fromisoformat(expiry.replace("Z", "+00:00"))
                horizon = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30)
                is_healthy = exp_dt > horizon
        except Exception:
            is_healthy = False

    if is_healthy and persisted:
        _step(f"  orchestrator webhook '{_ORCHESTRATOR_WEBHOOK_NAME}' healthy - reusing persisted URL")
        return persisted

    _step(f"  orchestrator webhook '{_ORCHESTRATOR_WEBHOOK_NAME}' missing/expired/lost-uri - minting")
    if show_rc == 0:
        _az([
            "az", "automation", "webhook", "delete",
            "--resource-group", aa_rg,
            "--automation-account-name", aa,
            "--name", _ORCHESTRATOR_WEBHOOK_NAME,
            "-y", "-o", "none",
        ])
    expiry_dt = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=365 * expiry_years)
    expiry_iso = expiry_dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    raw = _az([
        "az", "automation", "webhook", "create",
        "--resource-group", aa_rg,
        "--automation-account-name", aa,
        "--name", _ORCHESTRATOR_WEBHOOK_NAME,
        "--runbook-name", runbook_name,
        "--is-enabled", "true",
        "--expiry-time", expiry_iso,
        "-o", "json",
    ])
    try:
        resp = json.loads(raw or "{}")
        url_value = resp.get("uri") or resp.get("properties", {}).get("uri", "")
    except Exception:
        url_value = ""
    if not url_value:
        sys.exit(
            "ERROR: az automation webhook create did not return a 'uri' field. "
            "Without it we cannot wire the gateway. Inspect the AA in the portal."
        )
    _write_aa_variable(aa_rg, aa, _ORCHESTRATOR_WEBHOOK_VAR, url_value)
    _step(f"  orchestrator webhook minted; URL persisted as variable '{_ORCHESTRATOR_WEBHOOK_VAR}'.")
    return url_value


_CHDM_ADMIN_PRIVATE_KEY_FILENAME = "chdm_admin_id_ed25519"


def chdm_ensure_admin_private_key_file(
    repo_root: pathlib.Path, private_key_b64: str,
) -> pathlib.Path:
    """Decode AZ_VM_ADMIN_SSH_PRIVATE_KEY_B64 and land the OpenSSH private key
    file at Code/Shared/ops/certs/<filename> with mode 600. Operator runs
    `ssh -i <that path> chdm-admin@<vm-ip>` to admin the Hybrid Worker VM.
    Idempotent — overwrites on every call so a key rotation in .env
    propagates immediately to the operator's workstation."""
    certs_dir = repo_root / "Code" / "Shared" / "ops" / "certs"
    certs_dir.mkdir(parents=True, exist_ok=True)
    key_path = certs_dir / _CHDM_ADMIN_PRIVATE_KEY_FILENAME
    key_path.write_bytes(base64.b64decode(private_key_b64))
    key_path.chmod(0o600)
    _step(f"wrote admin SSH private key to {key_path} (mode 600)")
    return key_path


def chdm_set_functionapp_setting(fa_rg: str, fa_name: str, key: str, value: str) -> None:
    """Set a single app setting on a Function App (idempotent). Triggers
    a Function App restart automatically."""
    _step(f"setting Function App app setting on {fa_name}: {key}")
    _az([
        "az", "functionapp", "config", "appsettings", "set",
        "--resource-group", fa_rg, "--name", fa_name,
        "--settings", f"{key}={value}",
        "-o", "none",
    ])
