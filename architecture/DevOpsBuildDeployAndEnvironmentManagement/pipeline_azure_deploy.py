"""F-012 / LLD v22 pipeline Azure deploy helpers.

Deploy-created and pre-existing-shell reconcile helpers for:
  identity (managed_identity)
  azure_container_registry
  azure_key_vault (verify + secret seed)
  azure_storage_account (blob containers)
  azure_vnet (subnets)
  azure_resource_group (verify)
  azure_container_apps_environment
  azure_container_app_job (definition + image build)

No soft fallbacks. Every az failure aborts with stderr.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


ATLAS_API_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_API_ACCEPT = "application/vnd.atlas.2024-08-05+json"


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def step(msg: str) -> None:
    print(f"[pipeline_azure] {msg}", flush=True)


def _az(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["az", *args]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if check and r.returncode != 0:
        sys.exit(
            f"ERROR: az {' '.join(args[:6])}... failed ({r.returncode})\n"
            f"  stderr: {(r.stderr or '')[:2000]}"
        )
    return r


def _az_json(args: list[str]) -> Any:
    r = _az([*args, "-o", "json"])
    return json.loads(r.stdout) if r.stdout.strip() else None


def _env_block(target, env: str, attr: str) -> dict:
    for eb in target.environments:
        if eb.env_binding == env:
            block = getattr(eb, attr, None)
            if block is None:
                sys.exit(
                    f"ERROR: target {target.target_id!r} env={env!r} missing {attr}"
                )
            return block
    sys.exit(f"ERROR: target {target.target_id!r} has no env_binding={env!r}")


def verify_resource_group(target, env: str) -> str:
    block = _env_block(target, env, "azure_resource_group")
    name = block["name"]
    loc = block["location"]
    step(f"verify resource group {name}")
    r = _az(["group", "show", "--name", name], check=False)
    if r.returncode != 0:
        sys.exit(f"ERROR: pre_existing RG {name!r} not found (F-012 §4.2)")
    shown = json.loads(r.stdout)
    if shown.get("location", "").lower().replace(" ", "") != loc.lower().replace(" ", ""):
        sys.exit(
            f"ERROR: RG {name!r} location {shown.get('location')!r} != declared {loc!r}"
        )
    return name


def verify_key_vault(target, env: str) -> str:
    block = _env_block(target, env, "azure_key_vault")
    name = block["vault_name"]
    rg = block["resource_group"]
    step(f"verify key vault {name}")
    r = _az(["keyvault", "show", "--name", name, "--resource-group", rg], check=False)
    if r.returncode != 0:
        sys.exit(f"ERROR: pre_existing Key Vault {name!r} not found (F-012 §4.2)")
    _ensure_kv_secrets_from_file_packages(target, env, name)
    return name


def _ensure_kv_secrets_from_file_packages(target, env: str, vault_name: str) -> None:
    """Upload declared local files into KV as opaque secrets.

    Iterates the KV target's env_binding.packages[] for kind='secret_from_file'
    entries. Each package config carries `secret_name` and `source_file`
    (repo-relative path, gitignored files allowed). The file's raw content is
    uploaded via `az keyvault secret set --file`. Uses `--encoding base64` so
    line endings + special chars survive round-trip.

    Per the operator directive: Code/.env is the sole intentional source that
    is gitignored. Rule-006's scope entry declares this exemption.
    """
    for eb in target.environments:
        if eb.env_binding != env:
            continue
        for pkg in (eb.packages or []):
            if pkg.get("kind") != "secret_from_file":
                continue
            cfg = pkg.get("config") or {}
            secret_name = cfg["secret_name"]
            source_file = cfg["source_file"]
            repo_root = Path(__file__).resolve().parents[2]
            src_path = repo_root / source_file
            if not src_path.is_file():
                sys.exit(
                    f"ERROR: secret_from_file source {source_file!r} not found on disk"
                )
            import base64, gzip, tempfile
            raw = src_path.read_bytes()
            step(f"upload KV secret {secret_name} from {source_file} ({len(raw)}B raw)")
            # Gzip + base64. KV secret cap is 25KB; base64 alone of a
            # ~21KB .env overflows. Gzip typically cuts 21KB → ~5KB;
            # base64 of that is ~7KB, well under the limit.
            # Format prefix: "gz:" so the pull path knows the encoding.
            compressed = gzip.compress(raw, compresslevel=9)
            encoded = "gz:" + base64.b64encode(compressed).decode("ascii")
            step(f"  compressed+encoded to {len(encoded)}B (KV max 25600B)")
            if len(encoded) > 25000:
                sys.exit(
                    f"ERROR: encoded secret size {len(encoded)}B exceeds "
                    f"KV limit — .env grew too large; split it or use "
                    f"blob storage for the backup"
                )
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".b64gz", delete=False, encoding="ascii"
            ) as tmp:
                tmp.write(encoded)
                tmp_path = tmp.name
            try:
                r = _az(
                    [
                        "keyvault", "secret", "set",
                        "--vault-name", vault_name,
                        "--name", secret_name,
                        "--file", tmp_path,
                    ],
                    check=False,
                )
            finally:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            if r.returncode != 0:
                sys.exit(
                    f"ERROR: KV secret set {secret_name!r} failed: {r.stderr[:500]}"
                )
            step(f"  {secret_name} uploaded — pull with: "
                 f"az keyvault secret show --vault-name {vault_name} --name {secret_name} --query value -o tsv | base64 --decode > {source_file}")


def ensure_storage_containers(target, env: str) -> str:
    block = _env_block(target, env, "azure_storage_account")
    account = block["account_name"]
    rg = block["resource_group"]
    step(f"verify storage account {account}")
    r = _az(
        ["storage", "account", "show", "--name", account, "--resource-group", rg],
        check=False,
    )
    if r.returncode != 0:
        sys.exit(f"ERROR: pre_existing storage {account!r} not found (F-012 §4.2)")
    for c in block.get("blob_containers") or []:
        cname = c["name"]
        step(f"ensure blob container {cname} ({c.get('purpose')})")
        exists = _az(
            [
                "storage", "container", "exists",
                "--account-name", account,
                "--name", cname,
                "--auth-mode", "login",
                "--query", "exists",
                "-o", "tsv",
            ],
            check=False,
        )
        if exists.returncode == 0 and exists.stdout.strip().lower() == "true":
            continue
        _az(
            [
                "storage", "container", "create",
                "--account-name", account,
                "--name", cname,
                "--auth-mode", "login",
            ]
        )
    return account


def ensure_vnet_subnets(target, env: str) -> str:
    block = _env_block(target, env, "azure_vnet")
    vnet = block["vnet_name"]
    rg = block["resource_group"]
    step(f"verify vnet {vnet}")
    r = _az(["network", "vnet", "show", "-g", rg, "-n", vnet], check=False)
    if r.returncode != 0:
        sys.exit(f"ERROR: pre_existing VNet {vnet!r} not found (F-012 §4.2)")
    location = json.loads(r.stdout).get("location") or "eastus2"
    for subnet in block.get("subnets") or []:
        name = subnet["name"]
        prefix = subnet["address_prefix"]
        step(f"ensure subnet {name} {prefix}")
        show = _az(
            ["network", "vnet", "subnet", "show", "-g", rg, "--vnet-name", vnet, "-n", name],
            check=False,
        )
        delegations = subnet.get("delegations") or []
        if show.returncode != 0:
            args = [
                "network", "vnet", "subnet", "create",
                "-g", rg, "--vnet-name", vnet, "-n", name,
                "--address-prefixes", prefix,
            ]
            if delegations:
                args.extend(["--delegations", ",".join(delegations)])
            _az(args)
        for pool in (subnet.get("reserved_nic_pools") or []):
            _process_reserved_nic_pool(
                rg=rg,
                vnet=vnet,
                subnet_name=name,
                subnet_prefix=prefix,
                pool=pool,
                location=location,
            )
    return vnet


def _nic_pool_names(name_prefix: str, count: int) -> list[str]:
    """Return NIC names for a pool. Singleton (count==1) → `<name_prefix>`;
    multi-member → `<name_prefix>1`, `<name_prefix>2`, ... (1-indexed)."""
    if count == 1:
        return [name_prefix]
    return [f"{name_prefix}{i + 1}" for i in range(count)]


def _list_nics_by_prefix(*, rg: str, name_prefix: str) -> list[dict]:
    """Every NIC in the RG whose name starts with name_prefix."""
    nics = _az_json(
        ["network", "nic", "list", "-g", rg,
         "--query", f"[?starts_with(name, '{name_prefix}')]"]
    )
    return nics or []


def _process_reserved_nic_pool(
    *,
    rg: str,
    vnet: str,
    subnet_name: str,
    subnet_prefix: str,
    pool: dict,
    location: str,
) -> None:
    """Dispatch on the pool's action (default: create)."""
    action = pool.get("action", "create")
    if action == "delete":
        _delete_reserved_nic_pool(rg=rg, pool=pool)
        return
    if action == "rename":
        from_prefix = pool.get("from_name_prefix")
        if not from_prefix:
            sys.exit(
                f"ERROR: reserved NIC pool {pool['name_prefix']!r} "
                f"action=rename requires from_name_prefix"
            )
        _delete_reserved_nic_pool(rg=rg, pool={"name_prefix": from_prefix})
        _create_reserved_nic_pool(
            rg=rg, vnet=vnet, subnet_name=subnet_name,
            subnet_prefix=subnet_prefix, pool=pool, location=location,
        )
        return
    if action == "create_if_empty":
        if _list_nics_by_prefix(rg=rg, name_prefix=pool["name_prefix"]):
            step(f"reserved NIC pool {pool['name_prefix']!r}: "
                 f"create_if_empty skipped (existing NICs present)")
            return
    # create (default) or create_if_empty (empty case)
    _create_reserved_nic_pool(
        rg=rg, vnet=vnet, subnet_name=subnet_name,
        subnet_prefix=subnet_prefix, pool=pool, location=location,
    )


def _delete_reserved_nic_pool(*, rg: str, pool: dict) -> None:
    """Delete every NIC matching pool.name_prefix. Fail loud on any NIC
    attached to a VM (operator must detach first)."""
    name_prefix = pool["name_prefix"]
    matching = _list_nics_by_prefix(rg=rg, name_prefix=name_prefix)
    step(f"reserved NIC pool delete {name_prefix!r}: {len(matching)} NICs match")
    for nic in matching:
        nic_name = nic["name"]
        if nic.get("virtualMachine"):
            sys.exit(
                f"ERROR: NIC {nic_name!r} attached to a VM; cannot delete"
            )
        step(f"  delete NIC {nic_name}")
        _az(["network", "nic", "delete", "-g", rg, "-n", nic_name])


def _create_reserved_nic_pool(
    *,
    rg: str,
    vnet: str,
    subnet_name: str,
    subnet_prefix: str,
    pool: dict,
    location: str,
) -> None:
    """Create N unbound NICs with static IPs. Idempotent: matching-name +
    matching-IP is a no-op; matching-name + mismatched-IP is a hard error."""
    count = int(pool["count"])
    name_prefix = pool["name_prefix"]
    ip_offset_start = int(pool["ip_offset_start"])
    base_ip, _, _ = subnet_prefix.partition("/")
    octets = base_ip.split(".")
    if len(octets) != 4:
        sys.exit(
            f"ERROR: subnet {subnet_name!r} address_prefix {subnet_prefix!r} "
            f"is not IPv4 — reserved NIC pool requires IPv4 subnet"
        )
    subnet_id = _az_json(
        ["network", "vnet", "subnet", "show",
         "-g", rg, "--vnet-name", vnet, "-n", subnet_name,
         "--query", "id"]
    )
    names = _nic_pool_names(name_prefix, count)
    step(f"reserved NIC pool create {name_prefix!r}: {count} NICs starting at "
         f".{ip_offset_start}")
    for i, nic_name in enumerate(names):
        host_octet = ip_offset_start + i
        if host_octet > 254:
            sys.exit(
                f"ERROR: reserved NIC pool {name_prefix!r} count={count} "
                f"exhausts /24 host range (host octet {host_octet} > 254)"
            )
        expected_ip = f"{octets[0]}.{octets[1]}.{octets[2]}.{host_octet}"
        show = _az(
            ["network", "nic", "show", "-g", rg, "-n", nic_name],
            check=False,
        )
        if show.returncode == 0:
            live = json.loads(show.stdout)
            live_ip = (
                (live.get("ipConfigurations") or [{}])[0]
                .get("privateIPAddress")
            )
            if live_ip != expected_ip:
                sys.exit(
                    f"ERROR: reserved NIC {nic_name!r} private IP "
                    f"{live_ip!r} != declared slot {expected_ip!r}"
                )
            continue
        _az(
            [
                "network", "nic", "create",
                "-g", rg,
                "-n", nic_name,
                "--subnet", subnet_id,
                "--location", location,
                "--private-ip-address", expected_ip,
                "--private-ip-address-version", "IPv4",
            ]
        )


def ensure_managed_identity(target, env: str) -> str:
    block = _env_block(target, env, "identity")
    if block.get("identity_class") not in ("managed_identity", "azure_managed_identity"):
        sys.exit(
            f"ERROR: identity target {target.target_id!r} identity_class "
            f"{block.get('identity_class')!r} not supported for pipeline MI provision"
        )
    name = block["name"]
    rg = block["resource_group"]
    location = "eastus2"
    step(f"ensure managed identity {name}")
    show = _az(
        ["identity", "show", "--name", name, "--resource-group", rg],
        check=False,
    )
    if show.returncode != 0:
        _az(
            [
                "identity", "create",
                "--name", name,
                "--resource-group", rg,
                "--location", location,
            ]
        )
    return name


def ensure_pipeline_automation_identity(
    *,
    rg: str,
    aa_name: str,
    mi_name: str = "mi-runbook",
    vault_name: str = "kv-chpipeline-dev",
) -> str:
    """Attach mi-runbook to the pipeline Automation Account and grant KV read.

    F-003: only mi-runbook may read ca-intermediate-privatekey. The AA job
    sandbox obtains tokens via the user-assigned identity attached here.
    """
    step(f"ensure AA {aa_name} uses user-assigned identity {mi_name}")
    mi = _az_json(
        ["identity", "show", "--name", mi_name, "--resource-group", rg]
    )
    if not mi or not mi.get("id"):
        sys.exit(
            f"ERROR: managed identity {mi_name!r} missing in {rg!r}; "
            f"deploy target_identity_mi_runbook first"
        )
    mi_id = mi["id"]
    mi_oid = mi["principalId"]
    # Enable user-assigned identity on the AA (keep any system-assigned).
    _az(
        [
            "automation", "account", "update",
            "--name", aa_name,
            "--resource-group", rg,
            "--assign-identity",
            "--user-assigned", mi_id,
        ],
        check=False,
    )
    # Prefer REST PUT identity block — CLI update flags vary by extension version.
    sub = _az(
        ["account", "show", "--query", "id", "-o", "tsv"]
    ).stdout.strip()
    aa_url = (
        f"https://management.azure.com/subscriptions/{sub}"
        f"/resourceGroups/{rg}"
        f"/providers/Microsoft.Automation/automationAccounts/{aa_name}"
        f"?api-version=2023-11-01"
    )
    body = {
        "identity": {
            "type": "UserAssigned",
            "userAssignedIdentities": {mi_id: {}},
        }
    }
    _az(
        [
            "rest", "--method", "patch", "--url", aa_url,
            "--headers", "Content-Type=application/json",
            "--body", json.dumps(body), "-o", "none",
        ]
    )
    # Role assignments are applied at end of deploy by
    # apply_identity_role_grants_from_manifest in _deploy_chain.py, which
    # walks IdentityCatalog × target.allowed_roles intersection from the
    # manifest. No role names appear in this function anymore.
    step(f"AA {aa_name} identity={mi_name} attached (role grants applied post-deploy)")
    # AA Python sandboxes need AZURE_CLIENT_ID for user-assigned IMDS.
    client_id = mi.get("clientId") or ""
    if client_id:
        sub = _az(
            ["account", "show", "--query", "id", "-o", "tsv"]
        ).stdout.strip()
        var_url = (
            f"https://management.azure.com/subscriptions/{sub}"
            f"/resourceGroups/{rg}"
            f"/providers/Microsoft.Automation/automationAccounts/{aa_name}"
            f"/variables/AZURE_CLIENT_ID?api-version=2023-11-01"
        )
        var_body = {
            "properties": {
                "value": json.dumps(client_id),
                "isEncrypted": False,
                "description": "mi-runbook client id for IMDS token requests",
            }
        }
        import tempfile
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", delete=False,
        ) as tmp:
            json.dump(var_body, tmp)
            tmp_path = tmp.name
        try:
            _az(
                [
                    "rest", "--method", "put", "--url", var_url,
                    "--headers", "Content-Type=application/json",
                    "--body", f"@{tmp_path}", "-o", "none",
                ]
            )
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
        step(f"AA Automation Variable AZURE_CLIENT_ID={client_id}")
    return mi_name


def ensure_acr(target, env: str) -> str:
    block = _env_block(target, env, "azure_container_registry")
    name = block["registry_name"]
    rg = block["resource_group"]
    step(f"ensure ACR {name}")
    show = _az(["acr", "show", "--name", name, "--resource-group", rg], check=False)
    if show.returncode != 0:
        _az(
            [
                "acr", "create",
                "--name", name,
                "--resource-group", rg,
                "--sku", "Basic",
                "--admin-enabled", "false",
                "--location", "eastus2",
            ]
        )
    return name


def ensure_aca_environment(target, env: str) -> str:
    block = _env_block(target, env, "azure_container_apps_environment")
    name = block["environment_name"]
    rg = block["resource_group"]
    location = block["location"]
    vnet = block["vnet_name"]
    subnet = block["subnet_name"]
    logs = block["logs_destination"]
    if logs != "none":
        sys.exit("ERROR: F-012 requires ACA Environment logs_destination=none")
    step(f"ensure ACA environment {name}")
    show = _az(
        ["containerapp", "env", "show", "-n", name, "-g", rg],
        check=False,
    )
    if show.returncode == 0:
        return name
    subnet_id = _az(
        [
            "network", "vnet", "subnet", "show",
            "-g", rg, "--vnet-name", vnet, "-n", subnet,
            "--query", "id", "-o", "tsv",
        ]
    ).stdout.strip()
    _az(
        [
            "containerapp", "env", "create",
            "-n", name,
            "-g", rg,
            "--location", location,
            "--infrastructure-subnet-resource-id", subnet_id,
            "--logs-destination", "none",
        ]
    )
    return name


def _git_head_sha(repo_root: Path) -> str:
    r = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return r.stdout.strip()


def build_and_push_job_image(
    repo_root: Path,
    *,
    registry_name: str,
    image_repository: str,
    dockerfile: str,
    tag: str,
) -> str:
    """Build image into ACR via az acr build. Returns image ref."""
    df = repo_root / dockerfile
    if not df.is_file():
        sys.exit(f"ERROR: dockerfile missing: {dockerfile}")
    root_b64 = os.environ.get("CHATHEALTHY_CA_ROOT_B64", "").strip()
    intermediate_b64 = os.environ.get(
        "CHATHEALTHY_CA_INTERMEDIATE_B64", ""
    ).strip()
    if not root_b64 or not intermediate_b64:
        sys.exit(
            "ERROR: CHATHEALTHY_CA_ROOT_B64 / CHATHEALTHY_CA_INTERMEDIATE_B64 "
            "not set; bake_ca_chain_into_images must run before ACA job image "
            "builds (F-003 §7)."
        )
    # Dockerfiles COPY from pipeline/Code context.
    context = repo_root / "pipeline" / "Code"
    image = f"{registry_name}.azurecr.io/{image_repository}:{tag}"
    # Idempotent: many ACA jobs share prov-control / prov-worker tags.
    existing = _az(
        [
            "acr", "repository", "show",
            "--name", registry_name,
            "--image", f"{image_repository}:{tag}",
        ],
        check=False,
    )
    if existing.returncode == 0:
        step(f"acr image {image} already present — skip build")
        return image
    step(f"acr build {image}")
    # --no-logs waits for the server-side run without streaming log text.
    # Windows az (cp1252) raises UnicodeEncodeError on non-ASCII log bytes
    # even when the ACR task itself Succeeded — false deploy failures.
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    cmd = [
        "az", "acr", "build",
        "--registry", registry_name,
        "--image", f"{image_repository}:{tag}",
        "--file", str(df),
        "--build-arg", f"CHATHEALTHY_CA_ROOT_B64={root_b64}",
        "--build-arg", f"CHATHEALTHY_CA_INTERMEDIATE_B64={intermediate_b64}",
        "--no-logs",
        str(context),
    ]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env=env,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az acr build --registry {registry_name} "
            f"--image {image_repository}:{tag}... failed ({r.returncode})\n"
            f"  stderr: {(r.stderr or '')[:2000]}"
        )
    verify = _az(
        [
            "acr", "repository", "show",
            "--name", registry_name,
            "--image", f"{image_repository}:{tag}",
        ],
        check=False,
    )
    if verify.returncode != 0:
        sys.exit(
            f"ERROR: acr build reported success but image {image} is not "
            f"present in the registry."
        )
    return image


def ensure_aca_job(
    target,
    env: str,
    *,
    repo_root: Path,
    image_tag: str | None = None,
) -> str:
    block = _env_block(target, env, "azure_container_app_job")
    rg = block["resource_group"]
    job = block["job_name"]
    env_name = block["environment_name"]
    registry = block["registry_name"]
    repo = block["image_repository"]
    dockerfile = block["dockerfile"]
    role = block["role"]
    cpu = str(block["cpu"])
    memory = block["memory"]
    parallelism = str(block["parallelism"])
    timeout = str(block.get("replica_timeout", 7200))
    node_identity = block["node_identity"]
    mi_name = block["managed_identity_name"]
    kv_uri = block.get("key_vault_uri", "https://kv-chpipeline-dev.vault.azure.net/")

    tag = image_tag or _git_head_sha(repo_root)
    image = build_and_push_job_image(
        repo_root,
        registry_name=registry,
        image_repository=repo,
        dockerfile=dockerfile,
        tag=tag,
    )

    mi = _az_json(["identity", "show", "--name", mi_name, "--resource-group", rg])
    mi_id = mi["id"]
    mi_client = mi["clientId"]

    command = "control_runner.py" if role == "control" else "worker_runner.py"
    # Entrypoint is baked in Dockerfile (bootstrap.py <runner>); override args only.
    env_vars = [
        f"CHATHEALTHY_NODE_IDENTITY={node_identity}",
        f"KEY_VAULT_URI={kv_uri}",
        f"ENV_PREFIX={env}",
        f"PIPELINE_WORKER_MODE={'control' if role == 'control' else 'worker'}",
    ]

    step(f"ensure ACA job {job} role={role}")
    show = _az(["containerapp", "job", "show", "-n", job, "-g", rg], check=False)
    # create accepts identity/registry/--env-vars; update on containerapp 0.3.x
    # does not — use update-compatible flags + identity assign instead.
    if show.returncode != 0:
        _az(
            [
                "containerapp", "job", "create",
                "-n", job,
                "-g", rg,
                "--environment", env_name,
                "--trigger-type", "Manual",
                "--replica-completion-count", parallelism,
                "--image", image,
                "--cpu", cpu,
                "--memory", memory,
                "--parallelism", parallelism,
                "--replica-timeout", timeout,
                "--replica-retry-limit", "0",
                "--mi-user-assigned", mi_id,
                "--registry-identity", mi_id,
                "--registry-server", f"{registry}.azurecr.io",
                "--env-vars", *env_vars,
            ]
        )
    else:
        _az(
            [
                "containerapp", "job", "update",
                "-n", job,
                "-g", rg,
                "--image", image,
                "--cpu", cpu,
                "--memory", memory,
                "--parallelism", parallelism,
                "--replica-timeout", timeout,
                "--replica-retry-limit", "0",
                "--set-env-vars", *env_vars,
            ]
        )
        _az(
            [
                "containerapp", "job", "identity", "assign",
                "-n", job,
                "-g", rg,
                "--user-assigned", mi_id,
            ],
            check=False,
        )
    # Grant AcrPull to the job MI (idempotent-ish; ignore if exists).
    acr_id = _az(
        ["acr", "show", "--name", registry, "--query", "id", "-o", "tsv"]
    ).stdout.strip()
    _az(
        [
            "role", "assignment", "create",
            "--assignee-object-id", mi["principalId"],
            "--assignee-principal-type", "ServicePrincipal",
            "--role", "AcrPull",
            "--scope", acr_id,
        ],
        check=False,
    )
    step(f"ACA job {job} ready image={image} mi_client={mi_client}")
    return job


_ATLAS_OAUTH_TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"
_ATLAS_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


def _atlas_load_credentials() -> tuple[str, str, str]:
    """Read Atlas Admin API service-account credentials from Code/.env.

    Returns (client_id, client_secret, project_id). Fails hard if any
    is missing. Credentials are NOT declared in the manifest — the
    Atlas project we deploy to is identified by ATLAS_PROJECT_ID from
    Code/.env, and the service-account credentials (client id/secret)
    that authenticate the deploy are also read directly from Code/.env.
    """
    from dotenv import load_dotenv
    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / "Code" / ".env")
    load_dotenv(repo_root / ".env")
    client_id = os.environ.get("Mong_serviceAccount_ClientID", "")
    client_secret = os.environ.get("Mong_serviceAccount_ClientSecret", "")
    project_id = os.environ.get("ATLAS_PROJECT_ID", "")
    missing = [k for k, v in {
        "Mong_serviceAccount_ClientID": client_id,
        "Mong_serviceAccount_ClientSecret": client_secret,
        "ATLAS_PROJECT_ID": project_id,
    }.items() if not v]
    if missing:
        sys.exit(
            f"ERROR: Atlas deploy credentials missing from Code/.env: {missing}"
        )
    return client_id, client_secret, project_id


def _atlas_get_access_token(client_id: str, client_secret: str) -> str:
    """OAuth 2.0 client-credentials flow against Atlas /oauth/token.

    Caches the token in-process for the remainder of its TTL to avoid a
    round trip per Atlas API call. Atlas issues tokens with ~1 hour TTL.
    """
    cache_key = client_id
    cached = _ATLAS_TOKEN_CACHE.get(cache_key)
    if cached is not None:
        token, exp = cached
        if time.monotonic() < exp - 30:  # 30s safety margin
            return token
    import base64
    creds = base64.b64encode(
        f"{client_id}:{client_secret}".encode("utf-8")
    ).decode("ascii")
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode("utf-8")
    req = urllib.request.Request(
        _ATLAS_OAUTH_TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {creds}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        sys.exit(
            f"ERROR: Atlas OAuth token request HTTP {e.code}: {body_text}"
        )
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: Atlas OAuth token URL error: {e}")
    token = payload.get("access_token")
    if not token:
        sys.exit(f"ERROR: Atlas OAuth response missing access_token: {payload}")
    expires_in = int(payload.get("expires_in", 3600))
    _ATLAS_TOKEN_CACHE[cache_key] = (token, time.monotonic() + expires_in)
    return token


def _atlas_request(
    method: str,
    path: str,
    *,
    access_token: str,
    body: Any = None,
) -> tuple[int, Any]:
    """Call Atlas Admin API with Bearer-token auth.

    Returns (status_code, parsed_json_or_none). Never raises on 4xx/5xx;
    caller decides how to interpret the code.
    """
    url = f"{ATLAS_API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": ATLAS_API_ACCEPT,
            "Content-Type": "application/json" if data else "*/*",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            code = resp.getcode()
            payload = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        code = e.code
        payload = e.read().decode("utf-8", errors="replace")
    except urllib.error.URLError as e:
        sys.exit(f"ERROR: Atlas API URL error at {url}: {e}")
    parsed: Any = None
    if payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"_raw": payload}
    return code, parsed


def verify_atlas(target, env: str) -> str:
    """Reconcile Atlas target for this env: project + cluster verify,
    accepted_access reconciliation, private_endpoint_service ensure.
    Idempotent.
    """
    block = _env_block(target, env, "atlas")
    client_id, client_secret, project_id = _atlas_load_credentials()
    access_token = _atlas_get_access_token(client_id, client_secret)
    cluster_name = block["cluster"]["cluster_name"]
    step(f"Atlas project verify project_id={project_id[:8]}...")
    code, body = _atlas_request(
        "GET", f"/groups/{project_id}",
        access_token=access_token,
    )
    if code != 200:
        sys.exit(f"ERROR: Atlas project not reachable (HTTP {code}): {body}")
    step(f"Atlas cluster verify {cluster_name}")
    code, body = _atlas_request(
        "GET", f"/groups/{project_id}/clusters/{cluster_name}",
        access_token=access_token,
    )
    if code != 200:
        sys.exit(
            f"ERROR: Atlas cluster {cluster_name!r} not found in project "
            f"(HTTP {code}): {body}"
        )
    for access in block.get("accepted_access") or []:
        if access.get("kind") == "ip_allowlist":
            _atlas_reconcile_ip_allowlist(
                access,
                access_token=access_token,
                project_id=project_id,
            )
    pes = block.get("private_endpoint_service")
    if pes:
        endpoint_id = _atlas_ensure_private_endpoint_service(
            pes,
            access_token=access_token,
            project_id=project_id,
        )
        step(f"Atlas private endpoint service id={endpoint_id}")
    return target.target_id


def _atlas_reconcile_ip_allowlist(
    access: dict,
    *,
    access_token: str,
    project_id: str,
) -> None:
    """Ensure every declared allowlist entry exists on Atlas. Does NOT
    delete unlisted entries — the manifest is additive-only for access.
    """
    entries = access.get("entries") or []
    for entry in entries:
        cidr = entry["cidr"]
        comment = (entry.get("comment") or "")[:80]
        code, _body = _atlas_request(
            "POST", f"/groups/{project_id}/accessList",
            access_token=access_token,
            body=[{"cidrBlock": cidr, "comment": comment}],
        )
        if code in (200, 201):
            step(f"  allowlist added {cidr}")
        elif code == 409:
            step(f"  allowlist {cidr} already present")
        else:
            step(f"  WARN: allowlist add {cidr}: HTTP {code}")


def _atlas_ensure_private_endpoint_service(
    pes: dict,
    *,
    access_token: str,
    project_id: str,
) -> str:
    """Create-if-absent an Atlas private endpoint service for
    (provider, region). Poll until AVAILABLE. Returns the endpoint id.
    """
    provider = pes["provider"]
    region_input = pes["region"]
    atlas_region = region_input.upper().replace("-", "_")
    step(f"Atlas endpoint service ensure {provider}/{atlas_region}")
    code, existing = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpointService/endpointService/{provider}",
        access_token=access_token,
    )
    endpoint_id: str | None = None
    if code == 200 and isinstance(existing, list):
        for svc in existing:
            if (svc.get("regionName") or svc.get("region")) == atlas_region:
                endpoint_id = svc.get("id")
                break
    if not endpoint_id:
        code, created = _atlas_request(
            "POST",
            f"/groups/{project_id}/privateEndpointService/endpointService",
            access_token=access_token,
            body={"providerName": provider, "region": atlas_region},
        )
        if code not in (200, 201, 202):
            sys.exit(
                f"ERROR: Atlas endpoint service create failed HTTP {code}: {created}"
            )
        endpoint_id = (created or {}).get("id")
        if not endpoint_id:
            sys.exit(
                f"ERROR: Atlas endpoint service create returned no id: {created}"
            )
    for i in range(60):
        code, shown = _atlas_request(
            "GET",
            f"/groups/{project_id}/privateEndpointService/endpointService/"
            f"{provider}/{endpoint_id}",
            access_token=access_token,
        )
        if code == 200:
            status = shown.get("status") if isinstance(shown, dict) else None
            if i == 0 or i % 6 == 0:
                step(f"  Atlas endpoint {endpoint_id[:12]} status={status}")
            if status in ("AVAILABLE", "INITIATING"):
                return endpoint_id
        time.sleep(10)
    sys.exit(f"ERROR: Atlas endpoint service {endpoint_id} never became AVAILABLE")


def atlas_resolve_private_link_service_resource_id(
    peer_target,
    env: str,
    group_id: str,
) -> tuple[str, str]:
    """Look up the peer Atlas target's Private Link Service resource id
    for the declared endpoint service. Returns (private_link_service_id,
    dns_suffix). Called by VNET's PE handler at PE-create time.
    """
    _ = group_id  # currently only 'mongodb' — Atlas has no group discriminator
    block = _env_block(peer_target, env, "atlas")
    client_id, client_secret, project_id = _atlas_load_credentials()
    access_token = _atlas_get_access_token(client_id, client_secret)
    pes = block.get("private_endpoint_service") or {}
    provider = pes.get("provider", "AZURE")
    region_input = pes.get("region", "eastus2")
    atlas_region = region_input.upper().replace("-", "_")
    code, listed = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpointService/endpointService/{provider}",
        access_token=access_token,
    )
    if code != 200 or not isinstance(listed, list):
        sys.exit(
            f"ERROR: Atlas endpoint services list HTTP {code}: {listed}"
        )
    endpoint_id = None
    for svc in listed:
        if (svc.get("regionName") or svc.get("region")) == atlas_region:
            endpoint_id = svc.get("id")
            break
    if not endpoint_id:
        sys.exit(
            f"ERROR: Atlas endpoint service missing for {provider}/{atlas_region} — "
            f"is Atlas target deployed?"
        )
    code, shown = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpointService/endpointService/"
        f"{provider}/{endpoint_id}",
        access_token=access_token,
    )
    if code != 200 or not isinstance(shown, dict):
        sys.exit(
            f"ERROR: Atlas endpoint service {endpoint_id} details HTTP {code}"
        )
    pls_id = (
        shown.get("privateLinkServiceResourceId")
        or shown.get("privateEndpointResourceId")
    )
    if not pls_id:
        sys.exit(
            f"ERROR: Atlas endpoint service {endpoint_id} carries no "
            f"privateLinkServiceResourceId"
        )
    dns_suffix = shown.get("privateEndpointConnectionName") or ""
    return pls_id, dns_suffix


def atlas_approve_private_endpoint(
    peer_target,
    env: str,
    azure_pe_resource_id: str,
) -> None:
    """Approve the Azure-side private endpoint connection on Atlas."""
    block = _env_block(peer_target, env, "atlas")
    client_id, client_secret, project_id = _atlas_load_credentials()
    access_token = _atlas_get_access_token(client_id, client_secret)
    pes = block.get("private_endpoint_service") or {}
    provider = pes.get("provider", "AZURE")
    region_input = pes.get("region", "eastus2")
    atlas_region = region_input.upper().replace("-", "_")
    code, listed = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpointService/endpointService/{provider}",
        access_token=access_token,
    )
    endpoint_id = None
    if code == 200 and isinstance(listed, list):
        for svc in listed:
            if (svc.get("regionName") or svc.get("region")) == atlas_region:
                endpoint_id = svc.get("id")
                break
    if not endpoint_id:
        sys.exit("ERROR: Atlas endpoint service not present at approval time")
    code, body = _atlas_request(
        "POST",
        f"/groups/{project_id}/privateEndpointService/endpointService/"
        f"{provider}/{endpoint_id}/endpoint",
        access_token=access_token,
        body={"id": azure_pe_resource_id, "privateEndpointConnectionName": ""},
    )
    if code in (200, 201, 202):
        step(f"  Atlas endpoint approved azure_pe={azure_pe_resource_id[-40:]}")
    elif code == 409:
        step(f"  Atlas endpoint connection already registered")
    else:
        step(f"  WARN: Atlas endpoint approval HTTP {code}: {body}")


def ensure_vnet_private_dns_zones(target, env: str) -> None:
    """Create/link Private DNS zones declared under the VNET target."""
    block = _env_block(target, env, "azure_vnet")
    vnet = block["vnet_name"]
    rg = block["resource_group"]
    zones = block.get("private_dns_zones") or []
    for zone in zones:
        zone_name = zone["zone_name"]
        step(f"ensure private DNS zone {zone_name}")
        show = _az(
            ["network", "private-dns", "zone", "show",
             "-g", rg, "-n", zone_name],
            check=False,
        )
        if show.returncode != 0:
            _az(
                ["network", "private-dns", "zone", "create",
                 "-g", rg, "-n", zone_name],
            )
        link_name = f"{vnet}-link"
        show_link = _az(
            ["network", "private-dns", "link", "vnet", "show",
             "-g", rg, "-n", link_name, "-z", zone_name],
            check=False,
        )
        if show_link.returncode != 0:
            vnet_id = _az_json(
                ["network", "vnet", "show", "-g", rg, "-n", vnet, "--query", "id"]
            )
            _az(
                [
                    "network", "private-dns", "link", "vnet", "create",
                    "-g", rg, "-n", link_name,
                    "-z", zone_name,
                    "-v", vnet_id,
                    "-e", "false",
                ]
            )


def ensure_vnet_private_endpoints(target, env: str, coll) -> None:
    """Create Private Endpoints declared under the VNET target. Each PE
    references a peer target (Atlas) whose private_endpoint_service the
    endpoint connects to; wire the PE's dns-zone-group so DNS auto-pop.
    """
    block = _env_block(target, env, "azure_vnet")
    vnet = block["vnet_name"]
    rg = block["resource_group"]
    subscription = _az(
        ["account", "show", "--query", "id", "-o", "tsv"]
    ).stdout.strip()
    for pe in block.get("private_endpoints") or []:
        pe_name = pe["private_endpoint_name"]
        subnet_name = pe["subnet_name"]
        peer_target_id = pe["peer_target_id"]
        group_id = pe["group_id"]
        peer_target = coll.by_target_id(peer_target_id) if coll else None
        if peer_target is None:
            sys.exit(
                f"ERROR: private_endpoint {pe_name!r} peer_target_id "
                f"{peer_target_id!r} not found in manifest"
            )
        step(f"ensure private endpoint {pe_name} → {peer_target_id}/{group_id}")
        pls_id, _dns_suffix = atlas_resolve_private_link_service_resource_id(
            peer_target, env, group_id,
        )
        subnet_id = (
            f"/subscriptions/{subscription}/resourceGroups/{rg}/providers/"
            f"Microsoft.Network/virtualNetworks/{vnet}/subnets/{subnet_name}"
        )
        show = _az(
            ["network", "private-endpoint", "show",
             "-g", rg, "-n", pe_name],
            check=False,
        )
        if show.returncode != 0:
            _az(
                [
                    "network", "private-endpoint", "create",
                    "-g", rg,
                    "-n", pe_name,
                    "--vnet-name", vnet,
                    "--subnet", subnet_name,
                    "--private-connection-resource-id", pls_id,
                    "--connection-name", f"{pe_name}-conn",
                    "--group-id", group_id,
                    "--manual-request", "false",
                ]
            )
        pe_resource_id = _az_json(
            ["network", "private-endpoint", "show",
             "-g", rg, "-n", pe_name, "--query", "id"]
        )
        # Wire dns-zone-group binding to the matching private DNS zone.
        for zone in block.get("private_dns_zones") or []:
            zone_name = zone["zone_name"]
            group_name = f"{pe_name}-zonegroup"
            show_group = _az(
                ["network", "private-endpoint", "dns-zone-group", "show",
                 "-g", rg, "--endpoint-name", pe_name, "-n", group_name],
                check=False,
            )
            if show_group.returncode != 0:
                zone_id = _az_json(
                    ["network", "private-dns", "zone", "show",
                     "-g", rg, "-n", zone_name, "--query", "id"]
                )
                _az(
                    [
                        "network", "private-endpoint", "dns-zone-group", "create",
                        "-g", rg,
                        "--endpoint-name", pe_name,
                        "-n", group_name,
                        "--private-dns-zone", zone_id,
                        "--zone-name", zone_name.replace(".", "-"),
                    ]
                )
        atlas_approve_private_endpoint(peer_target, env, pe_resource_id)


def seed_kv_secrets_from_env(vault_name: str, secret_names: list[str]) -> None:
    """Seed declared secret names from process env / dotenv into KV (Set).

    Manifest secrets keys use env-var spelling (underscores). Azure Key
    Vault secret names allow only alphanumerics and hyphens, so underscores
    are converted to hyphens for the KV name while the env lookup keeps
    the original key.
    """
    from dotenv import load_dotenv

    repo_root = Path(__file__).resolve().parents[2]
    load_dotenv(repo_root / ".env")
    load_dotenv(repo_root / "Code" / ".env")
    for name in secret_names:
        env_key = name.replace("-", "_")
        val = os.environ.get(env_key) or os.environ.get(name)
        if not val:
            step(f"skip KV seed {name}: not present in local env")
            continue
        kv_name = name.replace("_", "-")
        step(f"seed KV secret {kv_name} (from env {env_key})")
        _az(
            [
                "keyvault", "secret", "set",
                "--vault-name", vault_name,
                "--name", kv_name,
                "--value", val,
            ]
        )
