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

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sys as _ch_sys, pathlib as _ch_pl
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / ".git").exists():
        _ch_lib = _ch_d / "ChatHealthyLib" / "src"
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
# The chain materialises the application .env, which sets
# CH_LOG_DESTINATION=mongo and CH_LOG_DB=pipelineAdmin. Those are the
# deployed application's facts, not this tool's: devops tooling runs on
# a workstation and its log is the operator's terminal. Inheriting them
# made a build depend on a Mongo write it has no grant for.
import os as _ch_os
_ch_os.environ["CH_LOG_DESTINATION"] = "stderr"
from chathealthy_lib.logging_service import ChatHealthyLoggingService
_CH_LOG = ChatHealthyLoggingService()


ATLAS_API_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_API_ACCEPT = "application/vnd.atlas.2023-01-01+json"


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def step(msg: str) -> None:
    _CH_LOG.info(f"[pipeline_azure] {msg}")


_AZ_ERROR_DIR = Path(tempfile.gettempdir()) / "pipeline_azure_deploy_errors"


def _az(args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["az", *args]
    r = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        # Persist the FULL command + full stderr + full stdout to a tmp file so
        # nothing is lost to truncation. Deploy chain summaries then point at
        # the file path for follow-up diagnosis.
        _AZ_ERROR_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        err_path = _AZ_ERROR_DIR / f"az_err_{ts}_{r.returncode}_{os.getpid()}.txt"
        try:
            err_path.write_text(
                f"# az call failed rc={r.returncode} at {ts}\n"
                f"# argv (full):\n{json.dumps(cmd, indent=2)}\n\n"
                f"# stdout:\n{r.stdout or '(empty)'}\n\n"
                f"# stderr:\n{r.stderr or '(empty)'}\n",
                encoding="utf-8",
            )
        except OSError:
            err_path = None
        summary = (
            f"ERROR: az {' '.join(args[:6])}... failed ({r.returncode})\n"
            f"  stderr (first 2000B): {(r.stderr or '')[:2000]}\n"
            f"  full stderr + argv:   {err_path}"
        )
        if check:
            sys.exit(summary)
        # Attach the summary + err_path so callers can surface both.
        r.summary = summary  # type: ignore[attr-defined]
        r.err_path = err_path  # type: ignore[attr-defined]
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

    Per the operator directive: .env is the sole intentional source that
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
                # Operator directive: KV holds exactly ONE version of this
                # secret at any time. Clear BOTH possible starting states
                # before set: active (delete + poll show-deleted) and
                # soft-deleted (purge + poll show-deleted gone). Prior
                # implementation only checked active; when the secret was
                # left in soft-delete by a prior failed deploy, set fired
                # cold and hit "deleted but recoverable" every time.
                import time as _time
                active = _az(
                    ["keyvault", "secret", "show", "--vault-name", vault_name,
                     "--name", secret_name, "--query", "id", "-o", "tsv"],
                    check=False,
                )
                if active.returncode == 0 and active.stdout.strip():
                    step(f"  deleting active {secret_name}")
                    _az(["keyvault", "secret", "delete", "--vault-name", vault_name,
                         "--name", secret_name], check=False)
                    for _ in range(60):
                        r = _az(["keyvault", "secret", "show-deleted",
                                 "--vault-name", vault_name, "--name", secret_name,
                                 "--query", "id", "-o", "tsv"], check=False)
                        if r.returncode == 0 and r.stdout.strip():
                            break
                        _time.sleep(2)
                soft_deleted = _az(
                    ["keyvault", "secret", "show-deleted", "--vault-name", vault_name,
                     "--name", secret_name, "--query", "id", "-o", "tsv"],
                    check=False,
                )
                if soft_deleted.returncode == 0 and soft_deleted.stdout.strip():
                    step(f"  purging soft-deleted {secret_name} (single-version policy)")
                    _az(["keyvault", "secret", "purge", "--vault-name", vault_name,
                         "--name", secret_name], check=False)
                    for _ in range(60):
                        r = _az(["keyvault", "secret", "show-deleted",
                                 "--vault-name", vault_name, "--name", secret_name,
                                 "--query", "id", "-o", "tsv"], check=False)
                        if r.returncode != 0 or not r.stdout.strip():
                            break
                        _time.sleep(2)
                # Bounded retry on Azure's post-purge server-side
                # eventual-consistency window. show-deleted already
                # returned empty (client view above), but the vault's
                # server-side "in progress" flag may not yet have
                # cleared, causing set to return "ObjectIsBeingDeleted".
                # Retry ONLY on that specific transient; any other
                # failure exits immediately.
                r = None
                for attempt in range(6):
                    r = _az(
                        [
                            "keyvault", "secret", "set",
                            "--vault-name", vault_name,
                            "--name", secret_name,
                            "--file", tmp_path,
                        ],
                        check=False,
                    )
                    if r.returncode == 0:
                        break
                    stderr_txt = (r.stderr or "")
                    is_soft_delete_race = (
                        "ObjectIsBeingDeleted" in stderr_txt
                        or "is currently being deleted" in stderr_txt
                    )
                    if not is_soft_delete_race:
                        break  # deterministic failure, no retry
                    step(
                        f"  {secret_name} set raced Azure soft-delete grace; "
                        f"retry {attempt + 1}/6 after 5s"
                    )
                    _time.sleep(5)
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
    location = block.get("location", "eastus2")
    step(f"ensure storage account {account}")
    r = _az(
        ["storage", "account", "show", "--name", account, "--resource-group", rg],
        check=False,
    )
    if r.returncode != 0:
        # Storage account doesn't exist — create it (v32 deploy_provisioned).
        _az([
            "storage", "account", "create",
            "--name", account,
            "--resource-group", rg,
            "--location", location,
            "--sku", "Standard_LRS",
            "--kind", "StorageV2",
            "--min-tls-version", "TLS1_2",
            "--allow-blob-public-access", "false",
            "--default-action", "Allow",
        ])
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
    address_space = block.get("address_space", "10.0.0.0/16")
    location = block.get("location", "eastus2")
    step(f"ensure vnet {vnet}")
    r = _az(["network", "vnet", "show", "-g", rg, "-n", vnet], check=False)
    if r.returncode != 0:
        # VNet doesn't exist — create it (v32 deploy_provisioned).
        _az([
            "network", "vnet", "create",
            "-g", rg,
            "-n", vnet,
            "--location", location,
            "--address-prefixes", address_space,
        ])
        r = _az(["network", "vnet", "show", "-g", rg, "-n", vnet], check=False)
    location = json.loads(r.stdout).get("location") or location
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


def ensure_managed_identity_from_config(block: dict, owner: str,
                                        label: str) -> str:
    """Provision one user-assigned managed identity from its config block.

    A managed identity is an Entra directory object, so the identities of
    the estate are packages inside one Entra target rather than a target
    each. `owner` and `label` name the target and package for diagnostics.
    """
    if block.get("identity_class") not in ("managed_identity", "azure_managed_identity"):
        sys.exit(
            f"ERROR: {owner}/{label} identity_class "
            f"{block.get('identity_class')!r} not supported for pipeline MI provision"
        )
    name = block["name"]
    rg = block["resource_group"]
    location = "eastus2"
    step(f"ensure managed identity {name} ({owner}/{label})")
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


def ensure_managed_identity(target, env: str) -> str:
    return ensure_managed_identity_from_config(
        _env_block(target, env, "identity"), target.target_id, env,
    )


def ensure_pipeline_automation_account(
    *,
    rg: str,
    aa_name: str,
) -> None:
    """Ensure the pipeline Automation Account exists.

    It carries no identity of its own. Each runbook authenticates as
    pipelineEditor from the credential the deploy places on it, so the account
    is a host and nothing more -- there is no identity attached here for a
    runbook to inherit, and none to be granted anything.
    """
    step(f"ensure automation account {aa_name}")
    if _az(
        ["automation", "account", "show", "--name", aa_name, "--resource-group", rg],
        check=False,
    ).returncode != 0:
        _az([
            "automation", "account", "create",
            "--name", aa_name,
            "--resource-group", rg,
            "--location", "eastus2",
            "--sku", "Basic",
        ])


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
    # v32 §3.1.9: the ACR target may carry docker_image packages (the
    # pipeline VM image). Build + push each one so the Pipeline Run VM
    # can pull it at cloud-init time.
    packages = _env_packages(target, env, "azure_container_registry")
    # Per Deployment Architecture v14: managed files live only in the
    # manifest (embedded_content). Materialize them into the source
    # tree just-in-time so the docker build context includes them, then
    # delete them from source tree after the ACR build finishes (finally
    # ensures cleanup even on build failure). Between deploys, the source
    # tree is empty of managed deployment-fact files.
    _repo_root = Path(__file__).resolve().parent.parent.parent
    _managed_paths = []
    for _pkg in packages:
        for _f in _pkg.get("files", []) or []:
            if _f.get("disposition") == "managed" and _f.get("source_location"):
                _managed_paths.append((_f["source_location"], _f.get("embedded_content") or ""))
    _wrote_paths = []
    try:
        for _rel, _content in _managed_paths:
            _out = _repo_root / _rel
            _out.parent.mkdir(parents=True, exist_ok=True)
            if _content.startswith("__base64__:"):
                import base64 as _b64_ext
                _out.write_bytes(_b64_ext.b64decode(_content[len("__base64__:"):], validate=True))
            else:
                _out.write_bytes(_content.encode("utf-8"))
            _wrote_paths.append(_out)
        step(f"  materialized {len(_wrote_paths)} managed file(s) for ACR build")
        _acr_docker_build_loop(packages, name, rg)
    finally:
        for _out in _wrote_paths:
            try:
                if _out.is_file():
                    _out.unlink()
            except OSError:
                pass
        if _wrote_paths:
            step(f"  cleaned {len(_wrote_paths)} managed file(s) from source tree")
    return name


def _acr_docker_build_loop(packages, name: str, rg: str) -> None:
    """Existing per-package docker_image build+push loop, extracted so
    the ensure_acr wrapper can bracket it with materialize+cleanup for
    v14 managed files."""
    for pkg in packages:
        if pkg.get("kind") != "docker_image":
            continue
        config = pkg.get("config", {}) or {}
        repo = config.get("image_repository") or pkg.get("package_id")
        dockerfile = config.get("dockerfile")
        build_context = config.get("build_context", ".")
        if not dockerfile:
            sys.exit(f"ERROR: ACR docker_image package {pkg.get('package_id')!r} missing dockerfile")
        # Content-hash gate: skip build+push if ACR already has an image
        # whose :latest tag digest matches the current content-hash tag
        # (i.e., a prior deploy already built + pushed this exact source).
        # Content hash covers Dockerfile bytes + every file under each
        # COPY-source path parsed from the Dockerfile itself (defensive:
        # picks up new COPY sources automatically when the Dockerfile
        # changes). Non-source paths in build_context ("." usually the
        # repo root) are NOT hashed — they're not baked into the image.
        content_hash = _dockerfile_context_hash(dockerfile, build_context)
        content_tag = f"content-{content_hash[:12]}"
        if _acr_image_current(name, rg, repo, "latest", content_tag):
            step(
                f"docker build+push SKIPPED — {name}/{repo}:latest already "
                f"points to content {content_hash[:12]} (source unchanged)"
            )
            continue

        step(f"docker build+push {name}.azurecr.io/{repo}:latest from {dockerfile}")
        # `az acr login` on Windows engages Docker's credential store, which
        # starts Docker Desktop. `az acr build` server-side does NOT need
        # this: it authenticates via the operator's current `az` session
        # directly. Skipping the login avoids Docker Desktop popup entirely.
        # Build args required by Dockerfile.control: CHATHEALTHY_CA_ROOT_B64,
        # CHATHEALTHY_CA_INTERMEDIATE_B64 (F-003 CA baked into image).
        # KV holds the raw PEM under `ca-root-cert` and `ca-intermediate-cert`;
        # base64-encode here for the docker build-arg.
        import base64 as _b64
        kv_name = "kv-chpipeline-dev"
        ca_root_pem = _az_secret(kv_name, "ca-root-cert", default="")
        ca_int_pem = _az_secret(kv_name, "ca-intermediate-cert", default="")
        if not ca_root_pem or not ca_int_pem:
            sys.exit(
                f"ERROR: KV {kv_name!r} missing ca-root-cert or "
                f"ca-intermediate-cert secrets (F-003 prerequisite)"
            )
        ca_root_b64 = _b64.b64encode(ca_root_pem.encode("utf-8")).decode("ascii")
        ca_int_b64 = _b64.b64encode(ca_int_pem.encode("utf-8")).decode("ascii")
        # `az acr build` performs both build and push server-side.
        # --no-logs avoids az CLI's log streamer choking on non-cp1252
        # characters in the remote build output (Windows charmap encoding
        # UnicodeEncodeError). Build succeeds or fails independently of
        # log streaming; failure surfaces via the ARM operation status.
        # Tag with BOTH :latest and :content-<hash> so the next deploy
        # can compare digests and skip if source is unchanged.
        # BUILDKIT_INLINE_CACHE=1 bakes layer-cache metadata into the
        # pushed image so subsequent `az acr build` runs can reuse
        # unchanged layers instead of rebuilding from base. First build
        # after this lands has no benefit (no prior cache metadata on
        # the existing image); every deploy AFTER that reuses layers
        # for anything upstream of the invalidating COPY. Typical:
        # base + pip install + ChatHealthyLib COPY stay cached
        # (~90% of image size); only the final pipeline/Code COPY
        # re-runs on Python source changes. Cuts ACR build from
        # ~5min to ~30-60s when only Python source changed.
        # Built here and pushed, rather than handed to ACR Tasks to build
        # server-side. `az acr build` needs listBuildSourceUploadUrl, which
        # lives in Contributor on the registry; the deploy identity holds
        # AcrPush, which is exactly the right to put an image there and
        # nothing more. Building locally keeps it that way.
        _acr_build_and_push(
            registry=name,
            repo=repo,
            tags=("latest", content_tag),
            dockerfile=dockerfile,
            build_context=build_context,
            build_args={
                "BUILDKIT_INLINE_CACHE": "1",
                "CHATHEALTHY_CA_ROOT_B64": ca_root_b64,
                "CHATHEALTHY_CA_INTERMEDIATE_B64": ca_int_b64,
            },
        )
    return name


def _acr_build_and_push(
    registry: str,
    repo: str,
    tags: tuple[str, ...],
    dockerfile: str,
    build_context: str,
    build_args: dict[str, str],
) -> None:
    """Build the image on this machine and push every tag to the registry.

    Authentication is `az acr login`, which mints a registry token from the
    signed-in identity -- no admin username or password, and no rights beyond
    AcrPush.
    """
    _az(["acr", "login", "--name", registry])
    refs = [f"{registry}.azurecr.io/{repo}:{t}" for t in tags]
    cmd = ["docker", "build", "--file", dockerfile]
    for k, v in build_args.items():
        cmd += ["--build-arg", f"{k}={v}"]
    for ref in refs:
        cmd += ["--tag", ref]
    cmd.append(build_context)
    env = os.environ.copy()
    env["DOCKER_BUILDKIT"] = "1"
    step(f"docker build {refs[0]}")
    r = subprocess.run(cmd, env=env, creationflags=_cflags(),
                       capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker build failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[-2000:]}"
        )
    for ref in refs:
        step(f"docker push {ref}")
        r = subprocess.run(["docker", "push", ref],
                           creationflags=_cflags(),
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(
                f"ERROR: docker push {ref} failed (exit {r.returncode})\n"
                f"  stderr: {(r.stderr or '').strip()[-2000:]}"
            )


def _dockerfile_context_hash(dockerfile_path: str, build_context: str) -> str:
    """Compute a stable sha256 covering the Dockerfile itself + every file
    under each `COPY <src>` path referenced by the Dockerfile.

    Only source paths that get baked into the image affect the hash —
    ambient files under the build_context that aren't COPY'd don't count.

    Excludes __pycache__ / .git / .pytest_cache to keep the hash stable
    across dev-machine noise."""
    df = Path(dockerfile_path).resolve()
    ctx = Path(build_context).resolve()
    h = hashlib.sha256()
    h.update(df.read_bytes())
    # Parse COPY <src> [<src2> ...] <dst> — take every token except the
    # last as a source path relative to build_context.
    copy_srcs: list[str] = []
    for line in df.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped.upper().startswith("COPY "):
            continue
        parts = stripped.split()
        if len(parts) < 3:
            continue
        copy_srcs.extend(parts[1:-1])
    _EXCLUDE_DIRS = {"__pycache__", ".git", ".pytest_cache", "node_modules", ".venv"}
    for src in copy_srcs:
        root = (ctx / src).resolve()
        if not root.exists():
            # COPY of a non-existent path would fail the build too; still
            # hash the missing-path token so the hash captures the request.
            h.update(f"MISSING:{src}\n".encode("utf-8"))
            continue
        if root.is_file():
            h.update(f"{root.relative_to(ctx)}\n".encode("utf-8"))
            h.update(root.read_bytes())
            continue
        for f in sorted(root.rglob("*")):
            if any(part in _EXCLUDE_DIRS for part in f.parts):
                continue
            if not f.is_file():
                continue
            h.update(f"{f.relative_to(ctx)}\n".encode("utf-8"))
            h.update(f.read_bytes())
    return h.hexdigest()


def _acr_image_current(
    registry: str, resource_group: str, repo: str,
    live_tag: str, content_tag: str,
) -> bool:
    """Return True iff registry/repo already carries both :live_tag and
    :content_tag AND they resolve to the same image digest. Meaning:
    the currently-live image is the current source. Safe to skip build.

    Returns False on any az error / missing tag / digest mismatch — the
    caller then rebuilds and re-pushes both tags."""
    live_digest = _acr_tag_digest(registry, resource_group, repo, live_tag)
    if not live_digest:
        return False
    content_digest = _acr_tag_digest(registry, resource_group, repo, content_tag)
    if not content_digest:
        return False
    return live_digest == content_digest


def _acr_tag_digest(
    registry: str, resource_group: str, repo: str, tag: str,
) -> str | None:
    """Query the sha256 digest for registry/repo:tag. Returns None if the
    tag doesn't exist or the az call fails."""
    r = _az(
        ["acr", "manifest", "show",
         "--name", f"{registry}/{repo}:{tag}",
         "--query", "digest", "-o", "tsv"],
        check=False,
    )
    if r.returncode != 0:
        return None
    return (r.stdout or "").strip() or None


def _az_secret(vault_name: str, secret_name: str, default: str = "") -> str:
    """Fetch a KV secret's value via az CLI; return default on any error."""
    r = _az(
        ["keyvault", "secret", "show",
         "--vault-name", vault_name,
         "--name", secret_name,
         "--query", "value", "-o", "tsv"],
        check=False,
    )
    if r.returncode != 0:
        return default
    return (r.stdout or "").strip()


def _env_packages(target, env: str, block_key: str) -> list:
    """Return the packages[] list for the given env binding, or []."""
    for eb in target.environments:
        if eb.env_binding != env:
            continue
        # environments block may hold packages either at env-level or
        # inside the typed block (depending on schema evolution).
        pkgs = getattr(eb, "packages", None)
        if pkgs:
            return list(pkgs)
        block = getattr(eb, block_key, None) or {}
        if isinstance(block, dict):
            pkgs = block.get("packages")
            if pkgs:
                return list(pkgs)
    return []


def ensure_aca_environment(target, env: str) -> str:
    block = _env_block(target, env, "azure_container_apps_environment")
    name = block["environment_name"]
    rg = block["resource_group"]
    location = block["location"]
    vnet = block["vnet_name"]
    subnet = block["subnet_name"]
    logs = block.get("logs_destination", "none")
    workspace_name = block.get("log_analytics_workspace", "").strip()

    if logs not in ("none", "log-analytics"):
        sys.exit(
            f"ERROR: logs_destination={logs!r} not supported; must be "
            f"'none' or 'log-analytics'."
        )
    if logs == "log-analytics" and not workspace_name:
        sys.exit(
            "ERROR: logs_destination='log-analytics' requires "
            "log_analytics_workspace to be set on the ACA env block."
        )

    step(f"ensure ACA environment {name} (logs={logs})")

    workspace_customer_id = ""
    workspace_shared_key = ""
    if logs == "log-analytics":
        from aca_helpers import aca_ensure_log_analytics_workspace  # noqa: PLC0415
        aca_ensure_log_analytics_workspace(
            workspace=workspace_name,
            resource_group=rg,
            location=location,
        )
        workspace_customer_id = _az(
            [
                "monitor", "log-analytics", "workspace", "show",
                "--workspace-name", workspace_name,
                "--resource-group", rg,
                "--query", "customerId", "-o", "tsv",
            ]
        ).stdout.strip()
        workspace_shared_key = _az(
            [
                "monitor", "log-analytics", "workspace", "get-shared-keys",
                "--workspace-name", workspace_name,
                "--resource-group", rg,
                "--query", "primarySharedKey", "-o", "tsv",
            ]
        ).stdout.strip()

    show = _az(
        ["containerapp", "env", "show", "-n", name, "-g", rg,
         "--query", "properties.appLogsConfiguration.destination",
         "-o", "tsv"],
        check=False,
    )
    if show.returncode == 0:
        current_dest = (show.stdout or "").strip().lower()
        target_dest = logs.replace("-", "").lower() if logs != "none" else "none"
        if current_dest == target_dest:
            step(f"  env {name} already has logs_destination={logs}")
            return name
        step(f"  env {name} logs_destination change: "
             f"{current_dest or 'none'} -> {logs}")
        update_cmd = [
            "containerapp", "env", "update",
            "-n", name, "-g", rg,
            "--logs-destination", logs,
        ]
        if logs == "log-analytics":
            update_cmd += [
                "--logs-workspace-id", workspace_customer_id,
                "--logs-workspace-key", workspace_shared_key,
            ]
        _az(update_cmd)
        return name

    subnet_id = _az(
        [
            "network", "vnet", "subnet", "show",
            "-g", rg, "--vnet-name", vnet, "-n", subnet,
            "--query", "id", "-o", "tsv",
        ]
    ).stdout.strip()
    create_cmd = [
        "containerapp", "env", "create",
        "-n", name,
        "-g", rg,
        "--location", location,
        "--infrastructure-subnet-resource-id", subnet_id,
        "--logs-destination", logs,
    ]
    if logs == "log-analytics":
        create_cmd += [
            "--logs-workspace-id", workspace_customer_id,
            "--logs-workspace-key", workspace_shared_key,
        ]
    _az(create_cmd)
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
    # Dockerfiles COPY pipeline/Code + ChatHealthyLib from repo-root context.
    context = repo_root
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
    # Container env vars are the merge of:
    #   1) computed shell facts (identity/KV URI/env prefix/worker mode)
    #   2) baked env vars from manifest (default_apps_environment +
    #      {role}_apps_environment merged at synth time by
    #      target_record._resolve_apps_env_vars, carried on
    #      azure_container_app_job.container_env_vars).
    # Later overrides earlier on name collision.
    env_vars_map: dict[str, str] = {
        "CHATHEALTHY_NODE_IDENTITY": node_identity,
        "KEY_VAULT_URI": kv_uri,
        "ENV_PREFIX": env,
        "PIPELINE_WORKER_MODE": "control" if role == "control" else "worker",
        "AZURE_CLIENT_ID": mi_client,
    }
    env_vars_map.update(block.get("container_env_vars") or {})
    env_vars = [f"{k}={v}" for k, v in env_vars_map.items()]

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
    # Confirm AcrPull on the job MI. The chain reads rather than grants.
    acr_id = _az(
        ["acr", "show", "--name", registry, "--query", "id", "-o", "tsv"]
    ).stdout.strip()
    _acr_role = _az(
        [
            "role", "assignment", "list",
            "--assignee", mi["principalId"],
            "--scope", acr_id,
            "--query", "[?roleDefinitionName=='AcrPull']",
            "-o", "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    # The chain reads rather than grants. Without AcrPull the job cannot pull
    # its image, so this is worth saying plainly rather than discovering at
    # the first pull.
    if not (getattr(_acr_role, "stdout", "") or "").strip().startswith("[{"):
        step(f"WARNING: {mi['principalId']} does not hold AcrPull on the registry; "
             f"grant it deliberately or the job cannot pull {image}")
    step(f"ACA job {job} ready image={image} mi_client={mi_client}")
    return job


_ATLAS_OAUTH_TOKEN_URL = "https://cloud.mongodb.com/api/oauth/token"
_ATLAS_TOKEN_CACHE: dict[str, tuple[str, float]] = {}


# Azure region name → Atlas region name mapping. Atlas uses a distinct
# region naming convention from Azure (e.g. Azure 'eastus2' → Atlas
# 'US_EAST_2'). Extend as new regions come into use.
_AZURE_TO_ATLAS_REGION: dict[str, str] = {
    "eastus": "US_EAST",
    "eastus2": "US_EAST_2",
    "westus": "US_WEST",
    "westus2": "US_WEST_2",
    "westus3": "US_WEST_3",
    "centralus": "CENTRAL_US",
    "northcentralus": "NORTH_CENTRAL_US",
    "southcentralus": "SOUTH_CENTRAL_US",
    "westcentralus": "WEST_CENTRAL_US",
    "canadacentral": "CANADA_CENTRAL",
    "canadaeast": "CANADA_EAST",
    "northeurope": "EUROPE_NORTH",
    "westeurope": "EUROPE_WEST",
    "uksouth": "UK_SOUTH",
    "ukwest": "UK_WEST",
    "eastasia": "ASIA_EAST",
    "southeastasia": "ASIA_SOUTHEAST",
    "japaneast": "JAPAN_EAST",
    "japanwest": "JAPAN_WEST",
    "australiaeast": "AUSTRALIA_EAST",
    "australiasoutheast": "AUSTRALIA_SOUTHEAST",
    "brazilsouth": "BRAZIL_SOUTH",
    "southafricanorth": "SOUTH_AFRICA_NORTH",
    "swedencentral": "SWEDEN_CENTRAL",
    "germanywestcentral": "GERMANY_WEST_CENTRAL",
    "koreacentral": "KOREA_CENTRAL",
    "centralindia": "INDIA_CENTRAL",
    "southindia": "INDIA_SOUTH",
    "westindia": "INDIA_WEST",
    "francecentral": "FRANCE_CENTRAL",
    "norwayeast": "NORWAY_EAST",
    "switzerlandnorth": "SWITZERLAND_NORTH",
    "uaenorth": "UAE_NORTH",
}


def _azure_to_atlas_region(region: str) -> str:
    """Return Atlas region name for a given Azure region."""
    lower = (region or "").lower()
    if lower in _AZURE_TO_ATLAS_REGION:
        return _AZURE_TO_ATLAS_REGION[lower]
    # Unmapped region — fail loud rather than guess incorrectly.
    sys.exit(
        f"ERROR: Azure region {region!r} not mapped to Atlas region name. "
        f"Add it to _AZURE_TO_ATLAS_REGION in pipeline_azure_deploy.py."
    )


def _atlas_load_credentials() -> tuple[str, str, str]:
    """Read Atlas Admin API service-account credentials from .env.

    Returns (client_id, client_secret, project_id). Fails hard if any
    is missing. Credentials are NOT declared in the manifest — the
    Atlas project we deploy to is identified by ATLAS_PROJECT_ID from
    .env, and the service-account credentials (client id/secret)
    that authenticate the deploy are also read directly from .env.
    """
    from dotenv import load_dotenv
    repo_root = Path(__file__).resolve().parents[2]
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
            f"ERROR: Atlas deploy credentials missing from .env: {missing}"
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
    atlas_region = _azure_to_atlas_region(region_input)
    step(f"Atlas endpoint service ensure {provider}/{atlas_region}")
    code, existing = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpoint/{provider}/endpointService",
        access_token=access_token,
    )
    endpoint_id: str | None = None
    # Atlas list endpoints may return either a bare array or a paginated
    # {"results": [...]} envelope. Accept both.
    listing: list = []
    if code == 200:
        if isinstance(existing, list):
            listing = existing
        elif isinstance(existing, dict) and isinstance(existing.get("results"), list):
            listing = existing["results"]
    for svc in listing:
        if (svc.get("regionName") or svc.get("region")) == atlas_region:
            endpoint_id = svc.get("id")
            break
    if not endpoint_id:
        code, created = _atlas_request(
            "POST",
            f"/groups/{project_id}/privateEndpoint/{provider}/endpointService",
            access_token=access_token,
            body={"region": atlas_region},
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
            f"/groups/{project_id}/privateEndpoint/{provider}/endpointService/"
            f"{endpoint_id}",
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
    atlas_region = _azure_to_atlas_region(region_input)
    code, listed = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpoint/{provider}/endpointService",
        access_token=access_token,
    )
    if code != 200:
        sys.exit(
            f"ERROR: Atlas endpoint services list HTTP {code}: {listed}"
        )
    listing = listed if isinstance(listed, list) else (
        listed.get("results") if isinstance(listed, dict) else []
    )
    endpoint_id = None
    for svc in (listing or []):
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
        f"/groups/{project_id}/privateEndpoint/{provider}/endpointService/"
        f"{endpoint_id}",
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
    atlas_region = _azure_to_atlas_region(region_input)
    code, listed = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpoint/{provider}/endpointService",
        access_token=access_token,
    )
    listing = listed if isinstance(listed, list) else (
        listed.get("results") if isinstance(listed, dict) else []
    )
    endpoint_id = None
    if code == 200:
        for svc in (listing or []):
            if (svc.get("regionName") or svc.get("region")) == atlas_region:
                endpoint_id = svc.get("id")
                break
    if not endpoint_id:
        sys.exit("ERROR: Atlas endpoint service not present at approval time")
    code, body = _atlas_request(
        "POST",
        f"/groups/{project_id}/privateEndpoint/{provider}/endpointService/"
        f"{endpoint_id}/endpoint",
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
                    # NB: no --group-id here. Atlas PLS is a THIRD-PARTY
                    # private link service; Azure rejects --group-id for
                    # those (GroupIdNotPermittedWhenConnectingToThirdPartyService).
                    # --group-id is only for Microsoft first-party services
                    # (keyvault/storage/etc.).
                    # v32 §3.18: Atlas-side PE peer object is hand-created; the
                    # deploy chain uses --manual-request true so the Azure side
                    # sits pending until atlas_approve_private_endpoint()
                    # approves it via the Atlas Admin API (no cross-tenant
                    # Azure auto-approval permission required).
                    "--manual-request", "true",
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
