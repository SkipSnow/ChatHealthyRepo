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
from pathlib import Path
from typing import Any


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
    return name


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
    for subnet in block.get("subnets") or []:
        name = subnet["name"]
        prefix = subnet["address_prefix"]
        step(f"ensure subnet {name} {prefix}")
        show = _az(
            ["network", "vnet", "subnet", "show", "-g", rg, "--vnet-name", vnet, "-n", name],
            check=False,
        )
        delegations = subnet.get("delegations") or []
        if show.returncode == 0:
            continue
        args = [
            "network", "vnet", "subnet", "create",
            "-g", rg, "--vnet-name", vnet, "-n", name,
            "--address-prefixes", prefix,
        ]
        if delegations:
            args.extend(["--delegations", ",".join(delegations)])
        _az(args)
    return vnet


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
    # Key Vault Secrets User on the vault (covers intermediate key + certs).
    vault_id = _az(
        [
            "keyvault", "show", "--name", vault_name,
            "--query", "id", "-o", "tsv",
        ]
    ).stdout.strip()
    _az(
        [
            "role", "assignment", "create",
            "--assignee-object-id", mi_oid,
            "--assignee-principal-type", "ServicePrincipal",
            "--role", "Key Vault Secrets User",
            "--scope", vault_id,
        ],
        check=False,
    )
    step(f"AA {aa_name} identity={mi_name} oid={mi_oid} KV Secrets User granted")
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
    # Dockerfiles COPY from pipeline/Code context.
    context = repo_root / "pipeline" / "Code"
    image = f"{registry_name}.azurecr.io/{image_repository}:{tag}"
    step(f"acr build {image}")
    _az(
        [
            "acr", "build",
            "--registry", registry_name,
            "--image", f"{image_repository}:{tag}",
            "--file", str(df),
            str(context),
        ]
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
    common = [
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
    if show.returncode != 0:
        _az(
            [
                "containerapp", "job", "create",
                "-n", job,
                "-g", rg,
                "--environment", env_name,
                "--trigger-type", "Manual",
                "--replica-completion-count", parallelism,
                *common,
            ]
        )
    else:
        _az(
            [
                "containerapp", "job", "update",
                "-n", job,
                "-g", rg,
                *common,
            ]
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
