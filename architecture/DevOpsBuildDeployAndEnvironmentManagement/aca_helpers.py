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

import functools
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

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


# Shared-fact loaders. Every per-target ACA fact (location, Dockerfile
# base image, placeholder image, Netherite EH topology) lives in the ACA
# target's environments[<env>].azure_container_app block in
# deployment_architecture.json (EPIC-008-F-012). No
# module-level constants for any of these.
#
# Multi-target support (F-012 v7 §5 Control + Worker Job definitions):
# the manifest carries one target_kind=azure_container_app record per
# distinct ACA Job (control, worker). Callers pass an explicit
# target_id or "all-aca-target-ids" iterators use aca_target_ids() to
# discover the full set from the manifest.

# Dockerfile ENV line stays in code because it's not a deploy fact — it's
# a runtime contract between the image and Azure Functions worker, and
# changes only when the worker contract changes.
_DOCKERFILE_ENV = (
    "AzureWebJobsScriptRoot=/home/site/wwwroot "
    "AzureFunctionsJobHost__Logging__Console__IsEnabled=true"
)


def _manifest_path() -> Path:
    return (
        Path(__file__).resolve().parents[2]
        / "brain" / "machine_artifacts" / "content"
        / "deployment_architecture.json"
    )


@functools.lru_cache(maxsize=1)
def _load_all_records() -> list[dict]:
    return json.loads(_manifest_path().read_text(encoding="utf-8"))["DeploymentTargetRecord"]


def aca_target_ids() -> list[str]:
    """Return every target_id in the manifest whose target_kind is
    azure_container_app. Replaces the single-target `_ACA_TARGET_ID`
    module constant. Callers iterate this when the operation applies
    to every ACA target (Control + Worker + future long-lived ACAs)."""
    return [
        rec["target_id"]
        for rec in _load_all_records()
        if rec.get("target_kind") == "azure_container_app"
    ]


@functools.lru_cache(maxsize=None)
def _load_aca_facts_for(target_id: str, env_binding: str | None = None) -> dict:
    """Return the ACA target's azure_container_app block for the named
    env_binding, or its first env_binding if none supplied. Fails loud
    if missing."""
    for rec in _load_all_records():
        if rec.get("target_id") != target_id:
            continue
        envs = rec.get("environments", [])
        if not envs:
            sys.exit(f"ERROR: target {target_id!r} has no environments[].")
        if env_binding is not None:
            for eb in envs:
                if eb.get("env_binding") == env_binding:
                    block = eb.get("azure_container_app")
                    if not block:
                        sys.exit(
                            f"ERROR: target {target_id!r} env_binding "
                            f"{env_binding!r} has no azure_container_app block."
                        )
                    return block
            sys.exit(
                f"ERROR: target {target_id!r} has no env_binding "
                f"{env_binding!r}."
            )
        block = envs[0].get("azure_container_app")
        if not block:
            sys.exit(
                f"ERROR: target {target_id!r} env_binding "
                f"{envs[0].get('env_binding')!r} has no azure_container_app block."
            )
        return block
    sys.exit(f"ERROR: target {target_id!r} not present in manifest.")


def _load_aca_facts(target_id: str | None = None) -> dict:
    """Back-compat shim. When no target_id supplied, defaults to the
    first ACA target in the manifest — matches the previous single-
    target behavior. New callers should pass the explicit target_id."""
    if target_id is None:
        ids = aca_target_ids()
        if not ids:
            sys.exit("ERROR: no azure_container_app targets in manifest.")
        target_id = ids[0]
    return _load_aca_facts_for(target_id)


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _step(msg: str) -> None:
    _CH_LOG.info(f"[aca] {msg}")


def aca_render_dockerfile() -> str:
    """Render the ACA Dockerfile bytes from in-code constants.

    Functions runtime sees function_app.py + host.json at
    /home/site/wwwroot/. Source bytes are staged under
    app/pipeline/Code/; the COPY pulls from that subpath so the layout
    on disk matches what gets baked into the image.
    """
    return (
        f"FROM {_load_aca_facts()['dockerfile_from']}\n"
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


def _ensure_event_hub(
    namespace: str,
    resource_group: str,
    eh: str,
    partition_count: int,
) -> None:
    """Generic ensure: present-and-matching → no-op, missing → create,
    present-but-wrong-count → fail loud. Partition count is IMMUTABLE on
    Azure Event Hubs — mismatch requires hand-delete + redeploy.
    """
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
            f"has partitionCount={existing_count}; deploy requires "
            f"{partition_count}.\n"
            f"  Azure Event Hubs partition count is IMMUTABLE — it cannot "
            f"be changed in place.\n"
            f"  Delete the Event Hub by hand, then re-run this deploy:\n"
            f"    az eventhubs eventhub delete --namespace-name {namespace} "
            f"--resource-group {resource_group} --name {eh}"
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


def aca_ensure_partitions_event_hub(
    namespace: str,
    resource_group: str,
    partition_count: int,
) -> None:
    """Ensure the Netherite 'partitions' Event Hub exists with the expected
    partition count.
    """
    n = _load_aca_facts()["netherite"]
    _ensure_event_hub(
        namespace, resource_group,
        n["partitions_eh_name"], partition_count,
    )


def aca_ensure_loadmonitor_event_hub(
    namespace: str,
    resource_group: str,
) -> None:
    """Ensure the Netherite loadmonitor Event Hub exists (1 partition,
    fixed by Netherite's transport layer)."""
    n = _load_aca_facts()["netherite"]
    _ensure_event_hub(
        namespace, resource_group,
        n["loadmonitor_eh_name"], n["loadmonitor_partition_count"],
    )


def aca_ensure_clients_event_hubs(
    namespace: str,
    resource_group: str,
) -> None:
    """Ensure the Netherite clients Event Hubs exist (names and per-Hub
    partition count fixed by Netherite's transport layer)."""
    n = _load_aca_facts()["netherite"]
    for name in n["clients_eh_names"]:
        _ensure_event_hub(
            namespace, resource_group,
            name, n["clients_partition_count"],
        )


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


def aca_ensure_log_analytics_workspace(
    workspace: str,
    resource_group: str,
    location: str | None = None,
) -> str:
    """Ensure a Log Analytics workspace exists. Returns its resource ID.

    Workspace-based App Insights and the ACA env's system-log sink both
    need a workspace; this helper is the one owning create-if-absent so
    every dependent helper can demand a resource ID and rely on it.
    """
    if location is None:
        location = _load_aca_facts()["location"]
    _step(
        f"verifying log analytics workspace '{workspace}' in resource "
        f"group '{resource_group}'"
    )
    show = subprocess.run(
        [
            "az", "monitor", "log-analytics", "workspace", "show",
            "--workspace-name", workspace,
            "--resource-group", resource_group,
            "--query", "id", "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if show.returncode == 0 and show.stdout.strip():
        _step(f"  workspace '{workspace}' exists — no-op")
        return show.stdout.strip()
    _step(f"  workspace '{workspace}' missing — creating")
    create = subprocess.run(
        [
            "az", "monitor", "log-analytics", "workspace", "create",
            "--workspace-name", workspace,
            "--resource-group", resource_group,
            "--location", location,
            "--query", "id", "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if create.returncode != 0 or not create.stdout.strip():
        sys.exit(
            f"ERROR: az monitor log-analytics workspace create failed "
            f"(exit {create.returncode})\n"
            f"  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    _step(f"  workspace '{workspace}' created.")
    return create.stdout.strip()


def aca_ensure_app_insights_component(
    component: str,
    resource_group: str,
    workspace_id: str,
    location: str | None = None,
) -> str:
    """Ensure a workspace-based App Insights component exists. Returns its
    connection string for env-var injection into the Container App.

    Workspace-based AI (the modern shape) writes its tables into the
    supplied workspace; classic AI is intentionally not used here.
    """
    if location is None:
        location = _load_aca_facts()["location"]
    _step(
        f"verifying app insights component '{component}' in resource "
        f"group '{resource_group}'"
    )
    show = subprocess.run(
        [
            "az", "monitor", "app-insights", "component", "show",
            "--app", component,
            "--resource-group", resource_group,
            "--query", "connectionString", "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if show.returncode == 0 and show.stdout.strip():
        _step(f"  app insights '{component}' exists — no-op")
        return show.stdout.strip()
    _step(f"  app insights '{component}' missing — creating (workspace-based)")
    create = subprocess.run(
        [
            "az", "monitor", "app-insights", "component", "create",
            "--app", component,
            "--resource-group", resource_group,
            "--location", location,
            "--workspace", workspace_id,
            "--kind", "web",
            "--query", "connectionString", "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if create.returncode != 0 or not create.stdout.strip():
        sys.exit(
            f"ERROR: az monitor app-insights component create failed "
            f"(exit {create.returncode})\n"
            f"  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    _step(f"  app insights '{component}' created.")
    return create.stdout.strip()


def aca_ensure_container_apps_environment(
    environment: str,
    resource_group: str,
    workspace: str,
    location: str | None = None,
) -> None:
    """Ensure the Container Apps Environment exists. System logs are routed
    into the supplied Log Analytics workspace."""
    if location is None:
        location = _load_aca_facts()["location"]
    _step(
        f"verifying container apps environment '{environment}' in resource "
        f"group '{resource_group}'"
    )
    show = subprocess.run(
        [
            "az", "containerapp", "env", "show",
            "--name", environment,
            "--resource-group", resource_group,
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if show.returncode == 0:
        _step(f"  container apps environment '{environment}' exists — no-op")
        return
    # The ACA env create command needs the workspace customer ID + shared
    # key, not the workspace resource ID. Fetch them from the workspace.
    cid = subprocess.run(
        [
            "az", "monitor", "log-analytics", "workspace", "show",
            "--workspace-name", workspace,
            "--resource-group", resource_group,
            "--query", "customerId", "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    keys = subprocess.run(
        [
            "az", "monitor", "log-analytics", "workspace", "get-shared-keys",
            "--workspace-name", workspace,
            "--resource-group", resource_group,
            "--query", "primarySharedKey", "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    customer_id = (cid.stdout or "").strip()
    shared_key = (keys.stdout or "").strip()
    if not customer_id or not shared_key:
        sys.exit(
            f"ERROR: could not read customerId / sharedKey for workspace "
            f"'{workspace}' (needed by az containerapp env create)."
        )
    _step(f"  container apps environment '{environment}' missing — creating")
    create = subprocess.run(
        [
            "az", "containerapp", "env", "create",
            "--name", environment,
            "--resource-group", resource_group,
            "--location", location,
            "--logs-workspace-id", customer_id,
            "--logs-workspace-key", shared_key,
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if create.returncode != 0:
        sys.exit(
            f"ERROR: az containerapp env create failed (exit {create.returncode})\n"
            f"  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    _step(f"  container apps environment '{environment}' created.")


def aca_ensure_container_app_exists(
    container_app: str,
    resource_group: str,
    environment: str,
    registry: str,
    min_replicas: int,
    max_replicas: int,
    cpu: float,
    memory_gi: float,
) -> None:
    """Ensure the Container App exists. Create with a placeholder image
    if missing; no-op if it already exists.

    First-create wires the ACR registry credentials so the subsequent
    aca_update_container_app can pull the real pipeline image. Scale and
    resource shape come straight from the target's azure_container_app
    block — no defaults, no fallbacks.
    """
    _step(
        f"verifying container app '{container_app}' in resource group "
        f"'{resource_group}'"
    )
    show = subprocess.run(
        [
            "az", "containerapp", "show",
            "--name", container_app,
            "--resource-group", resource_group,
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if show.returncode == 0:
        _step(f"  container app '{container_app}' exists — no-op")
        return

    user_env = f"ACR_{registry.upper().replace('-', '_')}_USERNAME"
    pwd_env = f"ACR_{registry.upper().replace('-', '_')}_PASSWORD"
    user = os.environ.get(user_env)
    pwd = os.environ.get(pwd_env)
    if not user or not pwd:
        sys.exit(
            f"ERROR: missing admin credentials for ACR '{registry}'.\n"
            f"  Required env vars: {user_env} and {pwd_env}."
        )

    _step(
        f"  container app '{container_app}' not present — creating with "
        f"placeholder image (deploy will replace with build image)"
    )
    create = subprocess.run(
        [
            "az", "containerapp", "create",
            "--name", container_app,
            "--resource-group", resource_group,
            "--environment", environment,
            "--image", _load_aca_facts()["placeholder_image"],
            "--registry-server", f"{registry}.azurecr.io",
            "--registry-username", user,
            "--registry-password", pwd,
            "--min-replicas", str(min_replicas),
            "--max-replicas", str(max_replicas),
            "--cpu", str(cpu),
            "--memory", f"{memory_gi}Gi",
            "--ingress", "external",
            "--target-port", "80",
            "-o", "none",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(),
        shell=(sys.platform == "win32"),
    )
    if create.returncode != 0:
        sys.exit(
            f"ERROR: az containerapp create failed (exit {create.returncode})\n"
            f"  stderr: {(create.stderr or '').strip()[:1500]}"
        )
    _step(f"  container app '{container_app}' created.")


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
    min_replicas: int,
    max_replicas: int,
    cpu: float,
    memory_gi: float,
) -> None:
    """Update the Container App image + env vars + replica/resource spec.

    Env vars sourced from secrets are passed as `<NAME>=secretref:<secret-name>`
    per ACA's secret-binding syntax. Plain env vars are passed as
    `<NAME>=<value>`.

    Replica and resource args MUST be passed on every update — without them
    the live Container App keeps whatever values it was first created with,
    which silently drifts from deployment_architecture.json. The
    min_replicas=0 vs 1 drift in particular causes Functions to fall back
    to its 5-minute scale-to-zero default, killing long-running activities.
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
        f"--set-env-vars <{len(env_pairs)} vars> "
        f"--min-replicas {min_replicas} --max-replicas {max_replicas} "
        f"--cpu {cpu} --memory {memory_gi}Gi"
    )
    args = [
        "az", "containerapp", "update",
        "--name", container_app,
        "--resource-group", resource_group,
        "--image", image_ref,
        "--set-env-vars", *env_pairs,
        "--min-replicas", str(min_replicas),
        "--max-replicas", str(max_replicas),
        "--cpu", str(cpu),
        "--memory", f"{memory_gi}Gi",
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
