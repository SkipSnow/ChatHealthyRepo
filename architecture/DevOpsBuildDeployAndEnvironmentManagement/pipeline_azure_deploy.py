"""F-012 / LLD v22 pipeline Azure deploy helpers.

Deploy-created and pre-existing-shell reconcile helpers for:
  identity (managed_identity)
  azure_container_registry
  azure_key_vault (verify + secret seed)
  azure_storage_account (blob containers)
  azure_vnet (subnets)
  azure_resource_group (verify)
  azure_container_apps_environment
  azure_container_app_job (refused: images are built at BUILD time)

No soft fallbacks. Every az failure aborts with stderr.
"""
from __future__ import annotations

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
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
_CH_LOG = ChatHealthyLoggingService()


ATLAS_API_BASE = "https://cloud.mongodb.com/api/atlas/v2"
ATLAS_API_ACCEPT = "application/vnd.atlas.2023-01-01+json"


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


import time as _time_mod
import threading as _threading


def step(msg: str) -> None:
    """Two clocks on every line -- seconds since the previous line and since
    the operator's invocation began -- and the line recorded on the chain
    timeline. A deploy used to go quiet for minutes with nothing saying
    whether that was a wait or a hang, and nothing recorded where the time
    went."""
    import chain_provenance as _cp  # noqa: PLC0415
    delta, total = _cp.timeline().mark("pipeline_azure", msg)
    _CH_LOG.info(f"[pipeline_azure +{delta:5.1f}s {total:6.1f}s] {msg}")


_AZ_ERROR_DIR = Path(tempfile.gettempdir()) / "pipeline_azure_deploy_errors"


def run_streamed(cmd: list[str], *, label: str, log_name: str,
                 env: dict | None = None, cwd: str | None = None,
                 shell: bool = False, heartbeat_s: float = 30.0) -> tuple[int, str, Path]:
    """Run a long subprocess with its output visible as it happens.

    Every line the child writes is echoed through step() the moment it
    arrives and appended to a transcript under _AZ_ERROR_DIR, so a failure
    still has the full text for post-mortem and a success no longer shows a
    blank screen for minutes. When the child is silent for heartbeat_s a
    line says so, with the elapsed time, so a live wait and a hang are
    distinguishable. Returns (returncode, combined_output, transcript_path).
    """
    import queue as _queue  # noqa: PLC0415
    _AZ_ERROR_DIR.mkdir(parents=True, exist_ok=True)
    transcript = _AZ_ERROR_DIR / f"{log_name}.log"
    lines: list[str] = []
    t0 = _time_mod.monotonic()
    q: "_queue.Queue[str | None]" = _queue.Queue()
    with open(transcript, "w", encoding="utf-8", errors="replace") as fh:
        fh.write("$ " + " ".join(cmd) + "\n")
        proc = subprocess.Popen(
            cmd, env=env, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=_cflags(), shell=shell,
        )

        def _pump() -> None:
            assert proc.stdout is not None
            for raw in proc.stdout:
                q.put(raw.rstrip("\r\n"))
            q.put(None)

        _threading.Thread(target=_pump, daemon=True).start()
        last_output = t0
        while True:
            try:
                item = q.get(timeout=heartbeat_s)
            except _queue.Empty:
                step(f"  [{label}] still running, no output for "
                     f"{_time_mod.monotonic() - last_output:.0f}s "
                     f"({_time_mod.monotonic() - t0:.0f}s total)")
                continue
            if item is None:
                break
            last_output = _time_mod.monotonic()
            fh.write(item + "\n")
            lines.append(item)
            if item.strip():
                step(f"  [{label}] {item}")
        rc = proc.wait()
        fh.write(f"# exit {rc}\n")
    step(f"  [{label}] exit {rc} after {_time_mod.monotonic() - t0:.0f}s "
         f"(transcript {transcript})")
    return rc, "\n".join(lines), transcript


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
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=summary)
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
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: target {target.target_id!r} env={env!r} missing {attr}")
            return block
    raise ChatHealthyException(
        mode="aborted",
        component="pipeline_azure_deploy",
        message=f"ERROR: target {target.target_id!r} has no env_binding={env!r}")


def verify_resource_group(target, env: str) -> str:
    block = _env_block(target, env, "azure_resource_group")
    name = block["name"]
    loc = block["location"]
    step(f"verify resource group {name}")
    r = _az(["group", "show", "--name", name], check=False)
    if r.returncode != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: pre_existing RG {name!r} not found (F-012 §4.2)")
    shown = json.loads(r.stdout)
    if shown.get("location", "").lower().replace(" ", "") != loc.lower().replace(" ", ""):
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: RG {name!r} location {shown.get('location')!r} != declared {loc!r}")
    return name


def verify_key_vault(target, env: str) -> str:
    block = _env_block(target, env, "azure_key_vault")
    name = block["vault_name"]
    rg = block["resource_group"]
    step(f"verify key vault {name}")
    r = _az(["keyvault", "show", "--name", name, "--resource-group", rg], check=False)
    if r.returncode != 0:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: pre_existing Key Vault {name!r} not found (F-012 §4.2)")
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
            import chain_provenance as _cp_root  # noqa: PLC0415
            repo_root = _cp_root.repository_root(Path(__file__))
            src_path = repo_root / source_file
            if not src_path.is_file():
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: secret_from_file source {source_file!r} not found on disk")
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
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: encoded secret size {len(encoded)}B exceeds "
                    f"KV limit — .env grew too large; split it or use "
                    f"blob storage for the backup")
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
                    for _poll in range(60):
                        r = _az(["keyvault", "secret", "show-deleted",
                                 "--vault-name", vault_name, "--name", secret_name,
                                 "--query", "id", "-o", "tsv"], check=False)
                        if r.returncode == 0 and r.stdout.strip():
                            break
                        step(f"    waiting for {secret_name} to reach soft-deleted "
                             f"(poll {_poll + 1}/60, {2 * (_poll + 1)}s/120s)")
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
                    for _poll in range(60):
                        r = _az(["keyvault", "secret", "show-deleted",
                                 "--vault-name", vault_name, "--name", secret_name,
                                 "--query", "id", "-o", "tsv"], check=False)
                        if r.returncode != 0 or not r.stdout.strip():
                            break
                        step(f"    waiting for {secret_name} purge to complete "
                             f"(poll {_poll + 1}/60, {2 * (_poll + 1)}s/120s)")
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
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: KV secret set {secret_name!r} failed: {r.stderr[:500]}")
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


def ensure_public_ip(target, env: str):
    """Reserve the VNET's declared Standard static Public IP (show-then-create)
    and return its allocated address. No-op returning None when the azure_vnet
    block declares no public_ip. The address is stable and ensure_controller_nic
    binds it to a permanent controller NIC, so the controller is reachable
    inbound on its status port the moment it attaches to that NIC."""
    block = _env_block(target, env, "azure_vnet")
    pip = block.get("public_ip")
    if not pip:
        return None
    rg = block["resource_group"]
    name = pip["name"]
    sku = pip["sku"]
    alloc = pip["allocation_method"]
    location = _az_json(["group", "show", "--name", rg]).get("location", "eastus2")
    step(f"ensure public ip {name} ({sku}/{alloc})")
    if _az(["network", "public-ip", "show", "-g", rg, "-n", name],
           check=False).returncode != 0:
        _az([
            "network", "public-ip", "create",
            "-g", rg, "-n", name,
            "--sku", sku,
            "--allocation-method", alloc,
            "--location", location,
        ])
    addr = _az_json(["network", "public-ip", "show", "-g", rg, "-n", name]).get("ipAddress", "")
    step(f"public ip {name} address = {addr}")
    # Reserving the IP and binding it to the permanent controller NIC are one
    # deployment deliverable, so the NIC mapping rides the same ensure call.
    ensure_controller_nic(target, env, location)
    return addr


def ensure_nat_gateway(target, env: str) -> None:
    """Ensure the declared egress NAT gateway: its own Standard static public
    IP, the gateway, and the association to the declared subnet. No-op when the
    azure_vnet block declares no nat_gateway.

    Root cause this realizes (2026-10-21): the run VMs egressed via Azure
    default outbound access, whose SNAT budget caps concurrent flows to ONE
    destination at ~1,024 ports. Every worker holds ~3 sockets to the
    front-end primary, so past ~350 workers each newborn's connect to exactly
    that host timed out (68/68 failures named it) while secondaries and other
    destinations connected fine via port reuse. A NAT gateway provides 64k
    SNAT ports per IP; Microsoft rates default outbound not-for-production.
    """
    block = _env_block(target, env, "azure_vnet")
    natgw = block.get("nat_gateway")
    if not natgw:
        return
    rg = block["resource_group"]
    vnet = block["vnet_name"]
    gw_name = natgw["name"]
    pip_name = natgw["public_ip_name"]
    subnet = natgw["subnet_name"]
    location = _az_json(["group", "show", "--name", rg]).get("location", "eastus2")
    step(f"ensure NAT gateway public ip {pip_name} (Standard/Static)")
    if _az(["network", "public-ip", "show", "-g", rg, "-n", pip_name],
           check=False).returncode != 0:
        _az(["network", "public-ip", "create", "-g", rg, "-n", pip_name,
             "--sku", "Standard", "--allocation-method", "Static",
             "--location", location])
    step(f"ensure NAT gateway {gw_name}")
    if _az(["network", "nat", "gateway", "show", "-g", rg, "-n", gw_name],
           check=False).returncode != 0:
        _az(["network", "nat", "gateway", "create", "-g", rg, "-n", gw_name,
             "--public-ip-addresses", pip_name, "--location", location])
    sub = _az_json(["network", "vnet", "subnet", "show", "-g", rg,
                    "--vnet-name", vnet, "-n", subnet])
    if not ((sub.get("natGateway") or {}).get("id") or "").endswith(f"/{gw_name}"):
        step(f"associate subnet {subnet} -> NAT gateway {gw_name}")
        _az(["network", "vnet", "subnet", "update", "-g", rg,
             "--vnet-name", vnet, "-n", subnet, "--nat-gateway", gw_name])
    step(f"NAT gateway {gw_name} serving subnet {subnet}")


def ensure_controller_nic(target, env: str, location: str) -> None:
    """Map the declared static public IP to a permanent controller NIC -- the
    VNET node the controller VM attaches to -- so inbound reach on the status
    port is a deployment deliverable, not per-run wiring. A direct route in: the
    public IP sits on the NIC itself, no load balancer and no NAT gateway. This
    is done once and left alone.

    Idempotent: creates the NSG and NIC only if absent; re-binds the public IP
    and NSG only when they are not already on the NIC. If a legacy egress NAT
    gateway still holds the public IP, it is dissociated and deleted first so
    the address is free to land on the NIC. No-op when no public_ip is declared.
    """
    block = _env_block(target, env, "azure_vnet")
    pip = block.get("public_ip")
    if not pip:
        return
    rg = block["resource_group"]
    vnet = block["vnet_name"]
    pip_name = pip["name"]
    nic_name = pip.get("controller_nic_name") or pip_name.replace("pip-", "nic-", 1)
    nsg_name = pip.get("status_nsg_name") or pip_name.replace("pip-", "nsg-", 1)
    status_port = str(pip.get("status_port") or 6969)
    landing = next((s["name"] for s in (block.get("subnets") or [])
                    if s.get("purpose") == "pipeline_run_vm_landing"), None)
    if not landing:
        raise ChatHealthyException(
            mode="manifest_incomplete",
            message="azure_vnet declares a public_ip but no subnet with purpose "
                    "'pipeline_run_vm_landing' to place the controller NIC in.",
            component="pipeline_azure_deploy")

    step(f"ensure status NSG {nsg_name} (inbound tcp {status_port})")
    if _az(["network", "nsg", "show", "-g", rg, "-n", nsg_name],
           check=False).returncode != 0:
        _az(["network", "nsg", "create", "-g", rg, "-n", nsg_name,
             "--location", location])
    if _az(["network", "nsg", "rule", "show", "-g", rg, "--nsg-name", nsg_name,
            "-n", "allow-status-in"], check=False).returncode != 0:
        _az(["network", "nsg", "rule", "create", "-g", rg, "--nsg-name", nsg_name,
             "-n", "allow-status-in", "--priority", "1000",
             "--access", "Allow", "--protocol", "Tcp", "--direction", "Inbound",
             "--source-address-prefixes", "Internet",
             "--destination-port-ranges", status_port])
    # ICMP inbound so the controller answers ping -- a coarse liveness probe
    # the operator can run before the mTLS API is up.
    if _az(["network", "nsg", "rule", "show", "-g", rg, "--nsg-name", nsg_name,
            "-n", "allow-icmp-in"], check=False).returncode != 0:
        _az(["network", "nsg", "rule", "create", "-g", rg, "--nsg-name", nsg_name,
             "-n", "allow-icmp-in", "--priority", "1010",
             "--access", "Allow", "--protocol", "Icmp", "--direction", "Inbound",
             "--source-address-prefixes", "Internet",
             "--destination-port-ranges", "*"])

    subnet_id = _az_json(["network", "vnet", "subnet", "show", "-g", rg,
                          "--vnet-name", vnet, "-n", landing, "--query", "id"])
    step(f"ensure controller NIC {nic_name} -> public ip {pip_name} (+NSG {nsg_name})")
    if _az(["network", "nic", "show", "-g", rg, "-n", nic_name],
           check=False).returncode != 0:
        _az(["network", "nic", "create", "-g", rg, "-n", nic_name,
             "--subnet", subnet_id, "--location", location,
             "--public-ip-address", pip_name,
             "--network-security-group", nsg_name])
    else:
        nic = _az_json(["network", "nic", "show", "-g", rg, "-n", nic_name])
        have_pip = ((nic.get("ipConfigurations") or [{}])[0]
                    .get("publicIPAddress") or {}).get("id") or ""
        if pip_name not in have_pip:
            step(f"bind public ip {pip_name} -> NIC {nic_name}")
            ipcfg = ((nic.get("ipConfigurations") or [{}])[0]).get("name") or "ipconfig1"
            _az(["network", "nic", "ip-config", "update", "-g", rg,
                 "--nic-name", nic_name, "-n", ipcfg, "--public-ip-address", pip_name])
        if nsg_name not in ((nic.get("networkSecurityGroup") or {}).get("id") or ""):
            step(f"bind NSG {nsg_name} -> NIC {nic_name}")
            _az(["network", "nic", "update", "-g", rg, "-n", nic_name,
                 "--network-security-group", nsg_name])


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
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: reserved NIC pool {pool['name_prefix']!r} "
                f"action=rename requires from_name_prefix")
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
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: NIC {nic_name!r} attached to a VM; cannot delete")
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: subnet {subnet_name!r} address_prefix {subnet_prefix!r} "
            f"is not IPv4 — reserved NIC pool requires IPv4 subnet")
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
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: reserved NIC pool {name_prefix!r} count={count} "
                f"exhausts /24 host range (host octet {host_octet} > 254)")
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
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: reserved NIC {nic_name!r} private IP "
                    f"{live_ip!r} != declared slot {expected_ip!r}")
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: {owner}/{label} identity_class "
            f"{block.get('identity_class')!r} not supported for pipeline MI provision")
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


def ensure_acr(target, env: str, package_selection=None) -> str:
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
    # Operator rulings 2026-10-22 (EPIC-010): the deploy MANUFACTURES NOTHING.
    # Images are built 100% complete at BUILD time (pipeline_image_build.py:
    # derived index JSONs, CA material, tags build-<n>/content-<hash>, and an
    # image_manifest.json in the build output). This deploy step only pushes
    # the already-built images and verifies the registry carries them.
    packages = _env_packages(target, env, "azure_container_registry")
    _acr_push_built_images(packages, name, rg, package_selection)
    return name


def _acr_push_built_images(packages, name: str, rg: str,
                           package_selection=None) -> None:
    """Push the images the BUILD manufactured. Reads each docker_image
    package's image_manifest.json from the CANONICAL build output -- the
    workstation's gitignored build/ directory, never the chain's checkout
    (nothing deploys from the repo; the deploy reads 100% build output,
    operator mandate 2026-10-22). A missing manifest or a missing local
    image is a hard fail -- the deploy never builds.

    --package is honoured here, not only when choosing targets. The
    registry is a host target, so it passes the target filter whole and
    then pushed every image package it declares: naming two of three
    images failed on the third, which was never asked for and so was
    never built. A package that WAS named and was not built is still a
    hard fail -- that is the build-first rule and a different fault."""
    import json as _json
    import chain_provenance as _cp  # noqa: PLC0415
    _repo_root = _cp.repository_root(Path(__file__))
    logged_in = False
    for pkg in packages:
        if pkg.get("kind") != "docker_image":
            continue
        pkg_id = pkg.get("package_id")
        if package_selection and pkg_id not in package_selection:
            continue
        manifest_path = (_repo_root / "build"
                         / "target_azure_container_registry_pipeline"
                         / str(pkg_id) / "image_manifest.json")
        if not manifest_path.is_file():
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: no image_manifest.json for package {pkg_id!r} "
                f"at {manifest_path}; the image was not built. The deploy "
                f"only deploys -- run the build first.")
        m = _json.loads(manifest_path.read_text(encoding="utf-8"))
        repo = m["image_repository"]
        build_tag = m["build_tag"]
        content_tag = m["content_tag"]
        local_ref = f"{repo}:{build_tag}"
        check = subprocess.run(["docker", "images", "-q", local_ref],
                               capture_output=True, text=True, shell=False)
        local_id = (check.stdout or "").strip()
        if not local_id:
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: built image {local_ref} (package {pkg_id!r}) "
                f"is not in the local docker daemon; rebuild before deploying.")
        if local_id != m.get("image_id"):
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: local {local_ref} is {local_id} but the build "
                f"manifest recorded {m.get('image_id')}; the daemon's image is "
                f"not the one the build produced. Rebuild before deploying.")
        if _acr_image_current(name, rg, repo, "latest", content_tag):
            step(f"docker push SKIPPED - {name}/{repo}:latest already points "
                 f"to content {content_tag} (image unchanged)")
            continue
        if not logged_in:
            _az(["acr", "login", "--name", name])
            logged_in = True
        for tag in ("latest", content_tag, build_tag):
            ref = f"{name}.azurecr.io/{repo}:{tag}"
            rc_t = subprocess.run(["docker", "tag", local_ref, ref],
                                  capture_output=True, text=True, shell=False)
            if rc_t.returncode != 0:
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: docker tag {local_ref} -> {ref} failed: "
                    f"{rc_t.stderr[:300]}")
            step(f"docker push {ref}")
            rc, out, log_path = run_streamed(
                ["docker", "push", ref], label="docker push",
                log_name=f"docker_push_{repo}_{tag}")
            if rc != 0:
                raise ChatHealthyException(
                    mode="aborted",
                    component="pipeline_azure_deploy",
                    message=f"ERROR: docker push {ref} failed (exit {rc}); "
                    f"full log: {log_path}; "
                    f"output tail: {out.strip()[-2000:]}")
        step(f"pushed {repo} build={m.get('build')} "
             f"(content {content_tag}) to {name}")


def _acr_image_current(
    registry: str, resource_group: str, repo: str,
    live_tag: str, content_tag: str,
) -> bool:
    """Return True iff registry/repo already carries both :live_tag and
    :content_tag AND they resolve to the same image digest. Meaning:
    the currently-live image is the current source. Safe to skip the push.

    Returns False on any az error / missing tag / digest mismatch — the
    caller then re-pushes the already-built image's tags (never rebuilds)."""
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: logs_destination={logs!r} not supported; must be "
            f"'none' or 'log-analytics'.")
    if logs == "log-analytics" and not workspace_name:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message="ERROR: logs_destination='log-analytics' requires "
            "log_analytics_workspace to be set on the ACA env block.")

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


def ensure_aca_job(
    target,
    env: str,
    *,
    repo_root: Path,
    image_tag: str | None = None,
) -> str:
    _env_block(target, env, "azure_container_app_job")  # validate the declaration
    raise ChatHealthyException(
        mode="not_implemented",
        component="pipeline_azure_deploy",
        message="azure_container_app_job deploy-time image build was removed "
        "(operator mandate 2026-10-22: images are built complete at BUILD "
        "time; the deploy only deploys). Declare the job's image as a "
        "docker_image package on the ACR target so the build manufactures it.")


def _azure_to_atlas_region(region: str) -> str:
    """Return Atlas region name for a given Azure region."""
    lower = (region or "").lower()
    if lower in _AZURE_TO_ATLAS_REGION:
        return _AZURE_TO_ATLAS_REGION[lower]
    # Unmapped region — fail loud rather than guess incorrectly.
    raise ChatHealthyException(
        mode="aborted",
        component="pipeline_azure_deploy",
        message=f"ERROR: Azure region {region!r} not mapped to Atlas region name. "
        f"Add it to _AZURE_TO_ATLAS_REGION in pipeline_azure_deploy.py.")


def _atlas_load_credentials() -> tuple[str, str, str]:
    """Read Atlas Admin API service-account credentials from .env.

    Returns (client_id, client_secret, project_id). Fails hard if any
    is missing. Credentials are NOT declared in the manifest — the
    Atlas project we deploy to is identified by ATLAS_PROJECT_ID from
    .env, and the service-account credentials (client id/secret)
    that authenticate the deploy are also read directly from .env.
    """
    from dotenv import load_dotenv
    import chain_provenance as _cp_root  # noqa: PLC0415
    repo_root = _cp_root.repository_root(Path(__file__))
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas deploy credentials missing from .env: {missing}")
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas OAuth token request HTTP {e.code}: {body_text}",
            exception=e)
    except urllib.error.URLError as e:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas OAuth token URL error: {e}",
            exception=e)
    token = payload.get("access_token")
    if not token:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas OAuth response missing access_token: {payload}")
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas API URL error at {url}: {e}",
            exception=e)
    parsed: Any = None
    if payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            parsed = {"_raw": payload}
    return code, parsed


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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas endpoint services list HTTP {code}: {listed}")
    listing = listed if isinstance(listed, list) else (
        listed.get("results") if isinstance(listed, dict) else []
    )
    endpoint_id = None
    for svc in (listing or []):
        if (svc.get("regionName") or svc.get("region")) == atlas_region:
            endpoint_id = svc.get("id")
            break
    if not endpoint_id:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas endpoint service missing for {provider}/{atlas_region} — "
            f"is Atlas target deployed?")
    code, shown = _atlas_request(
        "GET",
        f"/groups/{project_id}/privateEndpoint/{provider}/endpointService/"
        f"{endpoint_id}",
        access_token=access_token,
    )
    if code != 200 or not isinstance(shown, dict):
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas endpoint service {endpoint_id} details HTTP {code}")
    pls_id = (
        shown.get("privateLinkServiceResourceId")
        or shown.get("privateEndpointResourceId")
    )
    if not pls_id:
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message=f"ERROR: Atlas endpoint service {endpoint_id} carries no "
            f"privateLinkServiceResourceId")
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
        raise ChatHealthyException(
            mode="aborted",
            component="pipeline_azure_deploy",
            message="ERROR: Atlas endpoint service not present at approval time")
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
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: private_endpoint {pe_name!r} peer_target_id "
                f"{peer_target_id!r} not found in manifest")
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


def seed_kv_facts_from_manifest(vault_name: str, declared: dict,
                                identities: list[str] | None = None) -> None:
    """Write the vault facts the manifest states outright.

    A connection fact -- which host a cluster answers on, how many times a
    paused cluster is worth retrying -- is public and belongs in the record,
    not in a .env on somebody's workstation. Declared as
    literal:<value>, it travels with the manifest, is reviewed in a diff,
    and the deploy is what puts it in the vault. Nobody types an az command.

    The vault is the home rather than a collection because dbUtils reads
    these BEFORE it has a Mongo connection -- it is resolving where that
    connection goes. The vault is already the first login.
    """
    facts = {name: q.split(":", 1)[1]
             for name, q in (declared or {}).items()
             if isinstance(q, str) and q.startswith("literal:")}
    if not facts:
        return
    # Tagged onto each identity's own certificate secret, not written as
    # secrets of their own. The vault grants an identity exactly one secret
    # -- its certificate -- so a standalone mongo-host-* secret returns 403
    # to every caller that needs it, and granting it round would widen every
    # identity's reach. A tag rides on the read they already make.
    #
    # set-attributes --tags REPLACES all tags, so the manifest facts are
    # UNIONed on top of the secret's existing tags (grants-rights-for,
    # file-encoding, placed out-of-chain) rather than clobbering them. An
    # identity with no cert secret in this vault holds no Mongo credential
    # here and is skipped with a warning; any OTHER read failure is a real
    # error and aborts rather than being swallowed.
    required_host_facts = {n for n in facts if n.startswith("mongo-host-")}
    tagged: list[str] = []
    for identity in identities or []:
        show = _az(["keyvault", "secret", "show", "--vault-name", vault_name,
                    "--name", identity, "--query", "tags", "-o", "json"],
                   check=False)
        if show.returncode != 0:
            # az reports a missing secret and a refused one the same way -- a
            # non-zero exit -- so the message decides, matching the established
            # test in secrets_resolver.py. Only a recognisably-absent secret is
            # skipped; anything else (Unauthorized, vault unreachable) aborts.
            detail = ((show.stderr or "") + (show.stdout or "")).strip()
            if ("SecretNotFound" in detail
                    or "was not found in this key vault" in detail):
                step(f"skip tagging {identity}: no certificate secret in "
                     f"{vault_name} -- identity holds no Mongo credential here")
                continue
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: could not read tags of cert secret {identity!r} "
                        f"in {vault_name}, and this is not the vault reporting it "
                        f"absent: {detail[:600]}")
        existing = json.loads(show.stdout) if show.stdout.strip() else {}
        merged = {**(existing or {}), **facts}
        pairs = [f"{name}={value}" for name, value in merged.items()]
        step(f"tag {len(facts)} connection fact(s) onto cert secret {identity} "
             f"(merged over {len(existing or {})} existing tag(s))")
        _az(["keyvault", "secret", "set-attributes", "--vault-name", vault_name,
             "--name", identity, "--tags", *pairs])
        tagged.append(identity)
    # Fail loud, never silent: re-read each tagged identity's cert secret and
    # confirm every required mongo-host-* fact actually landed. A deploy that
    # leaves a Mongo-connecting identity without its host facts must abend
    # here, at deploy time -- not later at run time, where its absence raises
    # manifest_incomplete 'no host for cluster ...' in _host_for.
    for identity in tagged:
        verify = _az(["keyvault", "secret", "show", "--vault-name", vault_name,
                      "--name", identity, "--query", "tags", "-o", "json"],
                     check=False)
        tags = (json.loads(verify.stdout)
                if verify.returncode == 0 and verify.stdout.strip() else {}) or {}
        missing = sorted(n for n in required_host_facts
                         if not (tags.get(n) or "").strip())
        if missing:
            raise ChatHealthyException(
                mode="aborted",
                component="pipeline_azure_deploy",
                message=f"ERROR: cert secret {identity!r} in {vault_name} is "
                        f"missing required connection fact(s) {missing} after "
                        f"tagging; this identity would fail at run time with "
                        f"manifest_incomplete. Deploy aborts rather than leave it "
                        f"unable to connect to Mongo.")


def seed_kv_secrets_from_env(vault_name: str, secret_names: list[str]) -> None:
    """Seed declared secret names from process env / dotenv into KV (Set).

    Manifest secrets keys use env-var spelling (underscores). Azure Key
    Vault secret names allow only alphanumerics and hyphens, so underscores
    are converted to hyphens for the KV name while the env lookup keeps
    the original key.
    """
    from dotenv import load_dotenv

    import chain_provenance as _cp_root  # noqa: PLC0415
    repo_root = _cp_root.repository_root(Path(__file__))
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
