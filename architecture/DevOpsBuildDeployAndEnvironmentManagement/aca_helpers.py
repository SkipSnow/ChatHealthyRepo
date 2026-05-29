"""Azure Container Apps helpers for local_build / local_deploy.

Thin wrappers around `az` and `docker` for the `target_kind=azure_container_app`
build + deploy path. Each function fails loud — no try/except masking,
no soft fallbacks. The deploy is gospel truth; the operator gets the
real CLI error when something is wrong.

Track 2 of the v9 pipeline architecture migration: the Pipeline runs a
containerized Durable Functions Python worker with Netherite storage on
Azure Container Apps.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path


_DOCKERFILE_FROM = "mcr.microsoft.com/azure-functions/python:4-python3.11"
_DOCKERFILE_ENV = (
    "AzureWebJobsScriptRoot=/home/site/wwwroot "
    "AzureFunctionsJobHost__Logging__Console__IsEnabled=true"
)


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _step(msg: str) -> None:
    print(f"[aca] {msg}", flush=True)


def aca_render_dockerfile() -> str:
    """Render the ACA Dockerfile bytes from in-code constants.

    Functions runtime sees function_app.py + host.json at
    /home/site/wwwroot/. Source bytes are staged under
    app/pipeline/Code/; the COPY pulls from that subpath so the layout
    on disk matches what gets baked into the image.
    """
    return (
        f"FROM {_DOCKERFILE_FROM}\n"
        f"ENV {_DOCKERFILE_ENV}\n"
        "COPY app/pipeline/Code/ /home/site/wwwroot/\n"
        "RUN pip install --no-cache-dir -r /home/site/wwwroot/requirements.txt\n"
    )


def aca_content_hash_tree(tree_root: Path) -> str:
    """SHA256 over the staged tree's bytes, CRLF-normalized.

    Deterministic across Windows operator builds and Linux CI builds.
    File contents are CRLF -> LF normalized before hashing; the file's
    repo-relative path is mixed in so the hash is order-independent.
    """
    h = hashlib.sha256()
    for path in sorted(p for p in tree_root.rglob("*") if p.is_file()):
        rel = path.relative_to(tree_root).as_posix()
        h.update(rel.encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes().replace(b"\r\n", b"\n"))
        h.update(b"\0")
    return h.hexdigest()


_NETHERITE_PARTITIONS_EH_NAME = "partitions"


def aca_read_partition_count_from_host_json(repo_root: Path) -> int:
    """Read the Netherite partitionCount declared in pipeline/Code/host.json.

    host.json is the source of truth for the partition topology. Deploy
    uses this value to provision (or verify) the Event Hub that backs
    Netherite's partition queues.
    """
    host_json = repo_root / "pipeline" / "Code" / "host.json"
    if not host_json.is_file():
        sys.exit(f"ERROR: host.json not found at {host_json}")
    cfg = json.loads(host_json.read_text(encoding="utf-8"))
    try:
        return int(
            cfg["extensions"]["durableTask"]
               ["storageProvider"]["partitionCount"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        sys.exit(
            f"ERROR: could not read "
            f"extensions.durableTask.storageProvider.partitionCount "
            f"from {host_json}: {exc}"
        )


def aca_ensure_partitions_event_hub(
    namespace: str,
    resource_group: str,
    partition_count: int,
) -> None:
    """Ensure the Netherite 'partitions' Event Hub exists with the expected
    partition count.

    Three cases, no fallbacks:
      - missing      → create with N partitions
      - matching     → no-op
      - mismatched   → fail loud (Event Hub partition count is IMMUTABLE on
                       Azure; the operator must delete the Event Hub by hand
                       per the printed `az eventhubs eventhub delete` command,
                       then re-run deploy; the next deploy will recreate it
                       with the correct count).
    """
    eh = _NETHERITE_PARTITIONS_EH_NAME
    _step(
        f"verifying event hub '{eh}' in namespace '{namespace}' "
        f"(want partitionCount={partition_count})"
    )
    show = subprocess.run(
        [
            "az", "eventhubs", "eventhub", "show",
            "--namespace-name", namespace,
            "--resource-group", resource_group,
            "--name", eh,
            "-o", "json",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )

    if show.returncode == 0:
        existing = json.loads(show.stdout or "{}")
        existing_count = existing.get("partitionCount")
        if existing_count == partition_count:
            _step(
                f"  event hub '{eh}' exists with "
                f"partitionCount={partition_count} — no-op"
            )
            return
        sys.exit(
            f"ERROR: Event Hub '{eh}' in namespace '{namespace}' currently "
            f"has partitionCount={existing_count}; host.json requires "
            f"{partition_count}.\n"
            f"  Azure Event Hubs partition count is IMMUTABLE — it cannot "
            f"be changed in place.\n"
            f"  Delete the Event Hub by hand, then re-run this deploy:\n"
            f"    az eventhubs eventhub delete --namespace-name {namespace} "
            f"--resource-group {resource_group} --name {eh}\n"
            f"  The next deploy will recreate it with partitionCount="
            f"{partition_count}. NOTE: this drops every Netherite hub on "
            f"this namespace; ensure no live orchestrations remain first."
        )

    # show returned non-zero — assume not-found and create
    _step(
        f"  event hub '{eh}' not present — creating with "
        f"partitionCount={partition_count}"
    )
    create = subprocess.run(
        [
            "az", "eventhubs", "eventhub", "create",
            "--namespace-name", namespace,
            "--resource-group", resource_group,
            "--name", eh,
            "--partition-count", str(partition_count),
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if create.returncode != 0:
        sys.exit(
            f"ERROR: az eventhubs eventhub create failed "
            f"(exit {create.returncode})\n"
            f"  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    _step(f"  event hub '{eh}' created.")


def aca_ensure_netherite_storage_container(
    storage_account: str,
    storage_account_key: str,
    task_hub: str,
) -> None:
    """Ensure the Netherite Storage container for this TaskHub exists.

    Netherite persists partition state, checkpoints, and message logs in a
    blob container named `<lowercase task_hub>-storage` inside the
    AzureWebJobsStorage account. The container is auto-created lazily by
    the runtime, but lazy-create means stale state from a prior incarnation
    of the same TaskHub silently re-enters the new run. Deploy must own
    the existence check so the topology is reproducible.

    Behavior:
      - missing → create (the new TaskHub starts with empty Netherite state)
      - present → no-op (the operator hand-deletes if a fresh slate is
                  desired; deploy never wipes contents)
    """
    container_name = f"{task_hub.lower()}-storage"
    _step(
        f"verifying storage container '{container_name}' on account "
        f"'{storage_account}'"
    )
    show = subprocess.run(
        [
            "az", "storage", "container", "show",
            "--account-name", storage_account,
            "--account-key", storage_account_key,
            "--name", container_name,
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if show.returncode == 0:
        _step(f"  storage container '{container_name}' exists — no-op")
        return

    _step(f"  storage container '{container_name}' missing — creating")
    create = subprocess.run(
        [
            "az", "storage", "container", "create",
            "--account-name", storage_account,
            "--account-key", storage_account_key,
            "--name", container_name,
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if create.returncode != 0:
        sys.exit(
            f"ERROR: az storage container create failed "
            f"(exit {create.returncode})\n"
            f"  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    _step(f"  storage container '{container_name}' created.")


def aca_parse_storage_connection_string(conn_str: str) -> tuple[str, str]:
    """Extract AccountName and AccountKey from an Azure storage connection
    string. Fails loud if either is missing."""
    parts = {}
    for piece in conn_str.split(";"):
        if "=" in piece:
            k, v = piece.split("=", 1)
            parts[k.strip()] = v.strip()
    name = parts.get("AccountName")
    key = parts.get("AccountKey")
    if not name or not key:
        sys.exit(
            "ERROR: storage connection string is missing AccountName or "
            "AccountKey; cannot verify Netherite storage container."
        )
    return name, key


def aca_login_to_acr(registry: str) -> None:
    user_env = f"ACR_{registry.upper().replace('-', '_')}_USERNAME"
    pwd_env = f"ACR_{registry.upper().replace('-', '_')}_PASSWORD"
    user = os.environ.get(user_env)
    pwd = os.environ.get(pwd_env)
    if not user or not pwd:
        sys.exit(
            f"ERROR: missing admin credentials for ACR '{registry}'.\n"
            f"  Required env vars: {user_env} and {pwd_env}.\n"
            f"  These come from `az acr credential show -n {registry}` "
            f"and live in Code/.env (never committed)."
        )
    _step(f"docker login {registry}.azurecr.io -u {user} --password-stdin")
    r = subprocess.run(
        ["docker", "login", f"{registry}.azurecr.io", "-u", user, "--password-stdin"],
        input=pwd, capture_output=True, text=True,
        creationflags=_cflags(),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker login failed for {registry}.azurecr.io (exit {r.returncode}).\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}\n"
            f"  stdout: {(r.stdout or '').strip()[:300]}"
        )


def aca_docker_build(build_ctx: Path, image: str, build_n: int) -> str:
    image_tag = f"{image}:{build_n}"
    image_latest = f"{image}:latest"
    _step(f"docker build -t {image_tag} -t {image_latest} {build_ctx}")
    r = subprocess.run(
        ["docker", "build",
         "-t", image_tag, "-t", image_latest,
         str(build_ctx)],
        capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        creationflags=_cflags(),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: docker build failed for {image_tag}\n"
            f"{(r.stderr or r.stdout)[-2000:]}"
        )
    return image_tag


def aca_docker_push(image: str, build_n: int) -> None:
    for tag in (str(build_n), "latest"):
        ref = f"{image}:{tag}"
        _step(f"docker push {ref}")
        r = subprocess.run(
            ["docker", "push", ref],
            capture_output=True, text=True,
            encoding="utf-8", errors="replace",
            creationflags=_cflags(),
        )
        if r.returncode != 0:
            sys.exit(
                f"ERROR: docker push failed for {ref}\n"
                f"{(r.stderr or r.stdout)[-2000:]}"
            )


def _aca_secret_name(env_var_name: str) -> str:
    """Map an env var name to an ACA-legal secret slot name.

    ACA enforces: lowercase, alphanumeric + dashes, must start and end
    alphanumeric, max 20 chars. Many of our env vars (e.g.
    MONGO_FRONTEND_connectionString = 31 chars after dash-substitution)
    overrun the cap, so we truncate and strip trailing dashes. The
    env var name inside the container is unchanged; only the ACA-side
    secret slot uses the truncated form.
    """
    lowered = env_var_name.lower().replace("_", "-")
    truncated = lowered[:20].rstrip("-")
    return truncated or "x"


def aca_set_secrets(
    container_app: str,
    resource_group: str,
    secrets: dict[str, str],
) -> None:
    """Push secret values to the Container App's secret store.

    `az containerapp secret set` replaces named secrets in place; other
    secrets on the app are not touched. Secret names are lowercased
    here per ACA's naming rules.
    """
    if not secrets:
        _step("no secrets to set")
        return
    pairs = [f"{_aca_secret_name(k)}={v}" for k, v in secrets.items()]
    _step(
        f"az containerapp secret set --name {container_app} "
        f"--resource-group {resource_group} --secrets <{len(pairs)} secrets>"
    )
    args = [
        "az", "containerapp", "secret", "set",
        "--name", container_app,
        "--resource-group", resource_group,
        "--secrets", *pairs,
        "-o", "none",
    ]
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az containerapp secret set failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def aca_update_container_app(
    container_app: str,
    resource_group: str,
    image_ref: str,
    env_vars: dict[str, str],
    secret_names: list[str],
) -> None:
    """Update the Container App image + env vars in one revision.

    Env vars sourced from secrets are passed as `<NAME>=secretref:<secret-name>`
    per ACA's secret-binding syntax. Plain env vars are passed as
    `<NAME>=<value>`.
    """
    secret_set = set(secret_names)
    env_pairs: list[str] = []
    for name, value in env_vars.items():
        if name in secret_set:
            env_pairs.append(f"{name}=secretref:{_aca_secret_name(name)}")
        else:
            env_pairs.append(f"{name}={value}")
    _step(
        f"az containerapp update --name {container_app} "
        f"--resource-group {resource_group} --image {image_ref} "
        f"--set-env-vars <{len(env_pairs)} vars>"
    )
    args = [
        "az", "containerapp", "update",
        "--name", container_app,
        "--resource-group", resource_group,
        "--image", image_ref,
        "--set-env-vars", *env_pairs,
        "-o", "none",
    ]
    r = subprocess.run(
        args, capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az containerapp update failed (exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )


def aca_wait_for_revision(
    container_app: str,
    resource_group: str,
    timeout_s: int = 300,
) -> str:
    """Poll until the active revision is deployed and ready to serve.

    For ACA `min_replicas=0` apps a freshly-deployed revision sits at
    `ScaledToZero` (replicas=0) until the first inbound request lands;
    waiting for `runningState=='Running'` never succeeds in that case
    even though the revision is provisioned and ready. We accept either
    `Running` (warm) OR `ScaledToZero` (provisioned, scaled-to-zero) as
    the deploy-complete signal. We additionally require the revision's
    `provisioningState=='Provisioned'` so we don't accept a half-baked
    revision.

    Returns the active revision name. Fails loud on timeout.
    """
    deadline = time.time() + timeout_s
    last_state = "<none>"
    while time.time() < deadline:
        r = subprocess.run(
            ["az", "containerapp", "revision", "list",
             "--name", container_app,
             "--resource-group", resource_group,
             "-o", "tsv",
             "--query",
             "[?properties.active "
             "&& properties.provisioningState=='Provisioned' "
             "&& (properties.runningState=='Running' "
             "    || properties.runningState=='RunningAtMaxScale' "
             "    || properties.runningState=='ScaledToZero')].name"],
            capture_output=True, text=True,
            creationflags=_cflags(), shell=(sys.platform == "win32"),
        )
        if r.returncode == 0 and r.stdout.strip():
            rev = r.stdout.strip().splitlines()[0]
            _step(f"active revision provisioned: {rev}")
            return rev
        last_state = (r.stdout or "").strip() or (r.stderr or "").strip()
        _step(f"  waiting for active+provisioned revision … last={last_state!r}")
        time.sleep(10)
    sys.exit(
        f"ERROR: container app {container_app} did not report an active "
        f"provisioned revision within {timeout_s}s. last_state={last_state!r}"
    )


def aca_query_fqdn(container_app: str, resource_group: str) -> str:
    r = subprocess.run(
        ["az", "containerapp", "show",
         "--name", container_app,
         "--resource-group", resource_group,
         "--query", "properties.configuration.ingress.fqdn",
         "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0:
        sys.exit(
            f"ERROR: az containerapp show failed for {container_app} "
            f"(exit {r.returncode})\n"
            f"  stderr: {(r.stderr or '').strip()[:1500]}"
        )
    return r.stdout.strip()
