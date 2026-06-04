"""ChatHealthyDataMigrator deploy-side helpers.

Owns the check-or-configure logic for the persistent infrastructure the
migrator chain depends on:

  - Undelegated VM subnet inside ChatHealthy-VNet.
  - Hybrid Worker Group on the ChatHealthyJobManager Automation Account.
  - Role assignments on the AA's system-assigned managed identity
    (Virtual Machine Contributor + Automation Operator on the VM RG).
  - AA Python3 package list reconciled to the chdm-declared set (no
    hand actions; deploy owns the AA's Python environment).
  - Operator-landed SSH private key file for admin access to the
    ephemeral Hybrid Worker VM.

The orchestrator webhook URL is operator-minted once (long expiry) and
lives in Code/.env as MONGOCLUSTER_MIGRATOR_ORCHESTRATOR_WEBHOOK_URL;
the gateway target's secrets block pushes it to the Function App app
settings at gateway deploy time via the standard SecretsResolver flow.
No webhook code in this module.

Each function fails loud. No soft fallbacks; the operator sees the real
az error when something is wrong.
"""
from __future__ import annotations

import base64
import json
import pathlib
import subprocess
import sys
import time
import urllib.request


_AUTOMATION_API = "2023-11-01"
_NETWORK_API = "2024-01-01"
_VM_SUBNET_NAME = "hybrid-worker-subnet"
_VM_SUBNET_PREFIX = "10.0.2.0/24"
_VNET_NAME = "ChatHealthy-VNet"
_HYBRID_WORKER_GROUP = "ChatHealthyDataMigratorWorkGroup"

# The complete, declared AA Python3 package set required by the chdm
# runbooks running on the AA sandbox (orchestrator + provisioner +
# deprovisioner). All three import `requests` only. The migrator runs on
# the Hybrid Worker VM, NOT the AA sandbox, and gets its pymongo+dnspython
# from the VM's CustomScript extension - it does not contribute to this
# set. Versions are pinned to the latest pure-Python wheel compatible
# with the AA's Python 3.8 runtime.
_AA_PYTHON3_PACKAGES: dict[str, str] = {
    "requests": "2.32.3",
}


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


def _pypi_universal_wheel_url(package: str, version: str) -> str:
    """Look up the pure-Python wheel URL (py3-none-any) for package==version
    on PyPI. Pure-Python wheels load on any CPython version, so they sidestep
    the cp310-vs-cp38 wheel-platform problem that broke the prior AA state."""
    url = f"https://pypi.org/pypi/{package}/{version}/json"
    with urllib.request.urlopen(url, timeout=30) as resp:
        body = json.load(resp)
    for entry in body.get("urls", []):
        fn = entry.get("filename", "")
        if entry.get("packagetype") == "bdist_wheel" and fn.endswith("-py3-none-any.whl"):
            return entry["url"]
    raise RuntimeError(
        f"PyPI has no py3-none-any wheel for {package}=={version}. "
        f"Either pin a pure-Python version, or extend the deploy to pick "
        f"a runtime-compatible cp<N>-cp<N>-manylinux*.whl."
    )


def _aa_python3_packages_list(aa_rg: str, aa: str) -> dict[str, dict]:
    """Return current AA Python3 packages keyed by name. Values are
    {version, provisioningState, error}."""
    sub = _subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/python3Packages?api-version={_AUTOMATION_API}"
    )
    rc, out, _ = _az_try([
        "az", "rest", "--method", "get", "--url", url, "-o", "json",
    ])
    if rc != 0:
        return {}
    body = json.loads(out or "{}")
    out_map: dict[str, dict] = {}
    for p in body.get("value", []):
        props = p.get("properties", {}) or {}
        out_map[p.get("name")] = {
            "version": props.get("version"),
            "provisioningState": props.get("provisioningState"),
            "error": (props.get("error") or {}).get("message"),
        }
    return out_map


def _aa_python3_package_delete(aa_rg: str, aa: str, name: str) -> None:
    sub = _subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/python3Packages/{name}?api-version={_AUTOMATION_API}"
    )
    _step(f"  deleting AA Python3 package {name}")
    _az([
        "az", "rest", "--method", "delete", "--url", url, "-o", "none",
    ])


def _aa_python3_package_install(aa_rg: str, aa: str, name: str, version: str, wheel_url: str) -> None:
    sub = _subscription_id()
    url = (
        f"https://management.azure.com/subscriptions/{sub}/resourceGroups/{aa_rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa}"
        f"/python3Packages/{name}?api-version={_AUTOMATION_API}"
    )
    body = json.dumps({"properties": {"contentLink": {"uri": wheel_url}}})
    _step(f"  installing AA Python3 package {name}=={version} (wheel={wheel_url.split('/')[-1]})")
    _az([
        "az", "rest", "--method", "put", "--url", url,
        "--headers", "Content-Type=application/json",
        "--body", body, "-o", "none",
    ])
    # Poll until package install reaches a terminal state. The AA install is
    # asynchronous; provisioningState progresses Creating -> ContentDownloading
    # -> ContentValidating -> ContentStoring -> ContentDownloaded (terminal)
    # OR Failed (terminal). Anything else after 5 min is considered stuck.
    deadline = time.time() + 5 * 60
    while time.time() < deadline:
        cur = _aa_python3_packages_list(aa_rg, aa).get(name)
        state = (cur or {}).get("provisioningState")
        if state in ("Succeeded", "ContentDownloaded"):
            _step(f"  AA Python3 package {name}=={version} installed (state={state})")
            return
        if state == "Failed":
            err = (cur or {}).get("error") or "(no error message)"
            raise RuntimeError(
                f"AA Python3 package install FAILED for {name}=={version}: {err}"
            )
        time.sleep(10)
    raise RuntimeError(
        f"AA Python3 package install for {name}=={version} did not "
        f"reach a terminal state within 5min (last={state})"
    )


def chdm_ensure_aa_python3_packages(aa_rg: str, aa: str) -> None:
    """Reconcile the AA's Python3 package list to the chdm-declared set
    (_AA_PYTHON3_PACKAGES). Removes anything not declared; installs or
    replaces anything that is declared but missing or at the wrong version.
    All idempotent. After this returns, the AA's Python3 environment matches
    the declared state exactly. No hand actions required from the operator."""
    _step(f"verifying AA Python3 package set on '{aa}' matches chdm declaration")
    current = _aa_python3_packages_list(aa_rg, aa)
    declared = _AA_PYTHON3_PACKAGES

    # Remove extras (packages installed on AA that we no longer declare).
    for name in sorted(current.keys()):
        if name not in declared:
            _aa_python3_package_delete(aa_rg, aa, name)

    # Install missing or wrong-version declared packages.
    for name, want_version in declared.items():
        cur = current.get(name)
        if cur is None:
            wheel_url = _pypi_universal_wheel_url(name, want_version)
            _aa_python3_package_install(aa_rg, aa, name, want_version, wheel_url)
            continue
        if cur.get("version") != want_version or cur.get("provisioningState") not in ("Succeeded",):
            _aa_python3_package_delete(aa_rg, aa, name)
            wheel_url = _pypi_universal_wheel_url(name, want_version)
            _aa_python3_package_install(aa_rg, aa, name, want_version, wheel_url)
        else:
            _step(
                f"  AA Python3 package {name}=={want_version} already "
                f"installed (state={cur.get('provisioningState')})"
            )


_AA_TARGET_ID = "target_azure_automation_account_chathealthyjobmanager"


def _load_aa_target_entitlements() -> list[dict]:
    """Read the AA shared-infra target's entitlements[] from the brain
    artifact. Source-of-truth for what role assignments the deploy
    provisions on ChatHealthyJobManager (REQ-B-009).
    """
    repo_root = pathlib.Path(__file__).resolve().parents[2]
    manifest = repo_root / "brain" / "machine_artifacts" / "content" / "deployment_architecture.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    for rec in data["DeploymentTargetRecord"]:
        if rec.get("target_id") == _AA_TARGET_ID:
            return list(rec.get("entitlements", []))
    sys.exit(
        f"ERROR: target {_AA_TARGET_ID!r} not present in "
        f"deployment_architecture.json — cannot resolve AA role assignments."
    )


def chdm_ensure_chdm_persistent_infrastructure(
    *,
    vm_rg: str,
    aa_rg: str,
    aa: str,
) -> str:
    """Whole-of-infrastructure ensure. Called once per deploy session at
    the start of the first azure_automation_runbook deploy for a CHDM
    runbook. Returns the VM subnet ARM resource id.

    Role assignments are sourced from
    target_azure_automation_account_chathealthyjobmanager.entitlements[]
    (REQ-B-009: manifest is exact-truth for entitlements). Scope strings
    in the manifest carry templated tokens (`<subscription-id>`, `<vm_rg>`)
    that get substituted from the live deploy context here.
    """
    _step("=== verifying CHDM persistent infrastructure ===")
    subnet_id = chdm_ensure_vm_subnet(vm_rg)
    chdm_ensure_hybrid_worker_group(aa_rg, aa)

    sub = _subscription_id()
    aa_mi_pid = chdm_ensure_aa_managed_identity(aa_rg, aa)

    token_subs = {
        "<subscription-id>": sub,
        "<vm_rg>": vm_rg,
    }
    for entitlement in _load_aa_target_entitlements():
        if entitlement.get("kind") != "azure_rbac_role_assignment":
            continue
        if "system-assigned managed identity" not in entitlement.get("principal", ""):
            sys.exit(
                f"ERROR: entitlement principal {entitlement.get('principal')!r} "
                f"is not the AA's system-assigned managed identity — this deploy "
                f"path only handles the AA-MI. Update the manifest or extend "
                f"chdm_ensure_chdm_persistent_infrastructure."
            )
        scope = entitlement["scope"]
        for token, val in token_subs.items():
            scope = scope.replace(token, val)
        if "<" in scope:
            sys.exit(
                f"ERROR: unsubstituted token in scope {scope!r}; known tokens: "
                f"{sorted(token_subs)}."
            )
        chdm_ensure_role_assignment(aa_mi_pid, scope, entitlement["role"])

    chdm_ensure_aa_python3_packages(aa_rg, aa)

    return subnet_id


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


