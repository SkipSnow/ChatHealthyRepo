"""provider_pipeline_runbook.py -- trigger tier per LLD v22 §2.1.

Runs in Azure Automation as a Python 3 runbook. Fires from either:
  (a) a Schedule attached to the runbook
  (b) a webhook exposed by the Automation Account

Duties (LLD §2.6 steps 1-4):
  1. Read pipeline.config from chathealthyfrontend for pipeline_name='provider'
  2. Write a fresh run manifest to chathealthyfrontend.pipeline.runs with
     status='running', started_at=now, invocation_mode
  3. Call ARM API to start the prov-control ACA Job with env-var overrides:
     RUN_ID, ENV_PREFIX, INVOCATION_MODE, LOAD_MODE, STATE_SCOPE
  4. Exit. Do not wait for the pipeline to complete.

Every event is logged to the pipeline-logs blob container in our datalake
(one append blob per pipeline per day). Instrumentation is the runbook's
sole visibility surface -- operators reading the datalake blob see
exactly what the trigger tier did on each fire.

Design constraint: this runbook completes in seconds. All long-running
work happens inside Control + Workers. This dodges the 3-hour Azure
Automation fair-share cap.
"""
from __future__ import annotations

import datetime
import json
import os
import socket
import sys
import traceback
import urllib.error
import urllib.request
import uuid


# ---------- Deploy-time invariants (Automation Variables in prod) ----------
SUBSCRIPTION_ID = os.environ.get(
    "AUTOMATION_SUBSCRIPTION_ID", "7a17eec1-c477-4c7c-b1c1-d0662ce7a1ee"
)
RESOURCE_GROUP = os.environ.get(
    "AUTOMATION_RESOURCE_GROUP", "rg-chathealthy-pipeline-dev"
)
CONTROL_JOB_NAME = os.environ.get(
    "AUTOMATION_CONTROL_JOB_NAME", "job-chp-control-dev"
)
ENV_PREFIX = os.environ.get("AUTOMATION_ENV_PREFIX", "dev")
PIPELINE_NAME = "provider"

# Storage / Key Vault
LOG_ACCOUNT_URL = os.environ.get(
    "PIPELINE_LOG_ACCOUNT_URL", "https://stchpipelinedev.blob.core.windows.net"
)
LOG_CONTAINER = os.environ.get("PIPELINE_LOG_CONTAINER", "pipeline-logs")
KEY_VAULT_URI = os.environ.get(
    "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
)

# Webhook / cron defaults
INVOCATION_MODE = os.environ.get("INVOCATION_MODE", "scheduled")
LOAD_MODE_DEFAULT = os.environ.get("LOAD_MODE", "full")
STATE_SCOPE_DEFAULT = os.environ.get("STATE_SCOPE", "ALL")

_HOSTNAME = socket.gethostname()


# ============================================================================
# Managed-identity token acquisition
# ============================================================================
def _load_user_mi_client_id() -> str:
    """Fetch the user-assigned MI's client_id from Automation Variable
    AZURE_CLIENT_ID (populated by the deploy chain during AA identity
    attach). Falls back to os.environ for non-Automation contexts.
    Empty string if neither is available (system-MI attempt will then
    fail with a clear Azure error)."""
    try:
        import automationassets  # only present in Automation sandbox
        val = automationassets.get_automation_variable("AZURE_CLIENT_ID")
        if val:
            return str(val).strip()
    except Exception:
        pass
    return os.environ.get("AZURE_CLIENT_ID", "").strip()


AZURE_CLIENT_ID = _load_user_mi_client_id()


def _get_token(resource: str, client_id: str | None = None) -> str:
    """Fetch an OAuth2 token from IMDS for the given resource. Works in
    both Azure Automation Runbook sandbox and any container/VM with
    IMDS available.

    F-003: the AA carries only a user-assigned managed identity
    (mi-runbook). IMDS returns a system-MI token by default and Azure
    responds `Managed System Identity not found!` because no system-MI
    is attached. The user-assigned MI's client_id MUST be passed on
    the token query to select it. AZURE_CLIENT_ID is set as an
    Automation Variable by the deploy chain during AA identity attach."""
    cid = client_id or AZURE_CLIENT_ID
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT")
    identity_header = os.environ.get("IDENTITY_HEADER")
    if identity_endpoint and identity_header:
        q = f"?resource={resource}&api-version=2019-08-01"
        if cid:
            q += f"&client_id={cid}"
        url = f"{identity_endpoint}{q}"
        req = urllib.request.Request(url, headers={"X-IDENTITY-HEADER": identity_header})
    else:
        q = f"?api-version=2018-02-01&resource={resource}"
        if cid:
            q += f"&client_id={cid}"
        url = f"http://169.254.169.254/metadata/identity/oauth2/token{q}"
        req = urllib.request.Request(url, headers={"Metadata": "true"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


# ============================================================================
# Datalake blob logger -- writes to pipeline-logs/<pipeline>/YYYY-MM-DD.log
# ============================================================================
_blob_token_cache = {"token": None, "expires_at": 0}


def _blob_token() -> str:
    now = datetime.datetime.utcnow().timestamp()
    if _blob_token_cache["token"] and _blob_token_cache["expires_at"] > now + 30:
        return _blob_token_cache["token"]
    tok = _get_token("https://storage.azure.com/")
    _blob_token_cache["token"] = tok
    _blob_token_cache["expires_at"] = now + 3300  # ~55 min
    return tok


def _log_blob_url() -> str:
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    return f"{LOG_ACCOUNT_URL}/{LOG_CONTAINER}/{PIPELINE_NAME}/{today}.log"


def _ensure_append_blob():
    """Create the append blob if it doesn't exist yet. If it does, no-op."""
    url = _log_blob_url()
    tok = _blob_token()
    req = urllib.request.Request(
        url,
        method="PUT",
        headers={
            "Authorization": f"Bearer {tok}",
            "x-ms-blob-type": "AppendBlob",
            "x-ms-version": "2023-11-03",
            "Content-Length": "0",
            "If-None-Match": "*",  # only create if it doesn't already exist
        },
        data=b"",
    )
    try:
        with urllib.request.urlopen(req, timeout=15):
            pass
    except urllib.error.HTTPError as e:
        if e.code == 409:  # already exists -- that's fine
            return
        raise


_append_ready = False


def log(event: str, **fields):
    """Log one structured event both to stdout (so it shows in Automation
    job streams) AND append to the daily datalake blob."""
    global _append_ready
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    record = {
        "ts": now,
        "runbook": PIPELINE_NAME + "_pipeline_runbook",
        "hostname": _HOSTNAME,
        "event": event,
    }
    record.update(fields)
    line = json.dumps(record, default=str)
    print(line, flush=True)
    try:
        if not _append_ready:
            _ensure_append_blob()
            _append_ready = True
        url = _log_blob_url() + "?comp=appendblock"
        tok = _blob_token()
        body = (line + "\n").encode("utf-8")
        req = urllib.request.Request(
            url,
            method="PUT",
            headers={
                "Authorization": f"Bearer {tok}",
                "x-ms-version": "2023-11-03",
                "Content-Length": str(len(body)),
            },
            data=body,
        )
        with urllib.request.urlopen(req, timeout=15):
            pass
    except Exception as exc:
        # Never let logging break the runbook. Print the failure and move on.
        print(f"blob_log_failure: {type(exc).__name__}: {exc}",
              file=sys.stderr, flush=True)


# ============================================================================
# Mongo config + run manifest -- reads Mongo conn string from Key Vault
# ============================================================================
def _get_mongo_conn_string() -> str:
    """Fetch MONGO connection string from Key Vault. Secret was stored
    under the KV-legal name (dashes not underscores)."""
    name = os.environ.get("MONGO_SECRET_NAME", "MONGO-connectionString")
    tok = _get_token("https://vault.azure.net")
    url = f"{KEY_VAULT_URI.rstrip('/')}/secrets/{name}?api-version=7.4"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["value"]


def _doh_ssl_context():
    """SSL context for DoH -- uses certifi CA bundle if importable so the
    Automation sandbox's stripped-down trust store doesn't reject
    Cloudflare's cert chain."""
    import ssl
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _doh_query(name: str, rtype: str) -> list:
    """Query Cloudflare DNS-over-HTTPS for a single record type. Returns
    the Answer list (each item is a dict with `data` and `type`). Used
    to sidestep the Automation sandbox's broken outbound DNS resolver
    for Atlas SRV/TXT lookups."""
    url = f"https://cloudflare-dns.com/dns-query?name={name}&type={rtype}"
    req = urllib.request.Request(
        url, headers={"Accept": "application/dns-json"},
    )
    with urllib.request.urlopen(req, timeout=15, context=_doh_ssl_context()) as r:
        return json.loads(r.read().decode("utf-8")).get("Answer", []) or []


def _srv_to_direct_mongo_uri(srv_uri: str) -> str:
    """Translate a mongodb+srv:// URI to its non-SRV mongodb:// equivalent
    by resolving _mongodb._tcp.<host> SRV + <host> TXT via DNS-over-HTTPS,
    then reassembling the URI with the resolved hostnames and TXT options
    inline. Non-SRV inputs are returned unchanged."""
    if not srv_uri.startswith("mongodb+srv://"):
        return srv_uri
    tail = srv_uri[len("mongodb+srv://"):]
    if "@" in tail:
        userinfo, rest = tail.split("@", 1)
        userinfo = userinfo + "@"
    else:
        userinfo, rest = "", tail
    host_and_query = rest.split("/", 1)
    host_only = host_and_query[0]
    trailing = "/" + host_and_query[1] if len(host_and_query) > 1 else ""
    # SRV lookup for hosts:port
    srv_answers = _doh_query(f"_mongodb._tcp.{host_only}", "SRV")
    hosts = []
    for a in srv_answers:
        if a.get("type") != 33:  # SRV
            continue
        parts = a.get("data", "").strip().split()
        if len(parts) >= 4:
            port = parts[2]
            target = parts[3].rstrip(".")
            hosts.append(f"{target}:{port}")
    if not hosts:
        raise RuntimeError(f"DoH SRV lookup returned no hosts for {host_only}")
    # TXT lookup for connection options (Atlas ships tls/replicaSet/authSource)
    txt_answers = _doh_query(host_only, "TXT")
    txt_opts = ""
    for a in txt_answers:
        if a.get("type") != 16:  # TXT
            continue
        val = a.get("data", "").strip().strip('"')
        if val:
            txt_opts = val
            break
    # Assemble direct URI
    base = "mongodb://" + userinfo + ",".join(hosts) + trailing
    if txt_opts:
        sep = "&" if ("?" in base) else "?"
        base = base + sep + txt_opts
    # Atlas SRV implies tls=true; enforce.
    if "tls=" not in base and "ssl=" not in base:
        sep = "&" if ("?" in base) else "?"
        base = base + sep + "tls=true"
    return base


def _read_pipeline_config(mongo) -> dict:
    """LLD §2.6 step 2: read chathealthyfrontend.pipeline.config for
    pipeline_name='provider'. Returns config dict or empty dict if the
    document doesn't exist yet (first run)."""
    coll = mongo["chathealthyfrontend"]["pipeline.config"]
    doc = coll.find_one({"pipeline_name": PIPELINE_NAME, "env": ENV_PREFIX}) or {}
    return doc


def _write_run_manifest(mongo, run_id: str, load_mode: str,
                        state_scope, invocation_mode: str,
                        config: dict) -> None:
    """LLD §2.6 step 3: fresh manifest in chathealthyfrontend.pipeline.runs."""
    coll = mongo["chathealthyfrontend"]["pipeline.runs"]
    manifest = {
        "run_id": run_id,
        "pipeline_name": PIPELINE_NAME,
        "env": ENV_PREFIX,
        "status": "running",
        "started_at": datetime.datetime.utcnow(),
        "invocation_mode": invocation_mode,
        "load_mode": load_mode,
        "state_scope": state_scope,
        "config_snapshot": config,
    }
    coll.insert_one(manifest)


# ============================================================================
# ARM -- start the prov-control ACA Job with env-var overrides
# ============================================================================
def _start_control_job(run_id: str, load_mode: str, state_scope) -> dict:
    """LLD §2.6 step 4: POST to ACA job start endpoint with env overrides.

    The Microsoft.App/jobs/{name}/start action requires each container in
    the override to carry its FULL spec (image + resources), not just a
    delta. Fetch the job's definition first, extract the current template
    container, merge only the env overrides, then POST the full spec."""
    tok = _get_token("https://management.azure.com/")
    # 1) Fetch the current job definition to get container image + resources.
    get_url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.App/jobs/"
        f"{CONTROL_JOB_NAME}?api-version=2024-03-01"
    )
    get_req = urllib.request.Request(
        get_url,
        headers={"Authorization": f"Bearer {tok}"},
    )
    with urllib.request.urlopen(get_req, timeout=30) as r:
        job = json.loads(r.read().decode("utf-8"))
    template_containers = (
        (((job.get("properties") or {}).get("template")) or {}).get("containers") or []
    )
    if not template_containers:
        raise RuntimeError(
            f"ACA job {CONTROL_JOB_NAME!r} has no containers in template"
        )
    tpl = dict(template_containers[0])  # shallow copy
    # 2) Merge env overrides onto the existing env list.
    override_env = [
        {"name": "RUN_ID", "value": run_id},
        {"name": "ENV_PREFIX", "value": ENV_PREFIX},
        {"name": "INVOCATION_MODE", "value": INVOCATION_MODE},
        {"name": "LOAD_MODE", "value": load_mode},
        {"name": "STATE_SCOPE", "value": json.dumps(state_scope)},
        {"name": "PIPELINE_NAME", "value": PIPELINE_NAME},
    ]
    override_names = {e["name"] for e in override_env}
    merged_env = [e for e in (tpl.get("env") or []) if e.get("name") not in override_names]
    merged_env.extend(override_env)
    tpl["env"] = merged_env
    # 3) POST full container spec.
    start_url = (
        f"https://management.azure.com/subscriptions/{SUBSCRIPTION_ID}"
        f"/resourceGroups/{RESOURCE_GROUP}/providers/Microsoft.App/jobs/"
        f"{CONTROL_JOB_NAME}/start?api-version=2024-03-01"
    )
    payload = {"containers": [tpl]}
    req = urllib.request.Request(
        start_url,
        method="POST",
        headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"},
        data=json.dumps(payload).encode("utf-8"),
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode("utf-8"))


# ============================================================================
# Webhook input parsing
# ============================================================================
def _parse_webhook_input() -> dict:
    """Webhook payload discovery across every mechanism Azure Automation
    might use for Python-3 runbooks:
      - WEBHOOKDATA env var (documented Python-3 mechanism)
      - sys.argv[1] as a JSON string (PowerShell convention)
      - other env vars containing WEBHOOK/AUTOMATION/TRIGGER/INPUT
      - stdin (non-blocking read)
    Emits a diagnostic event listing every source and their (truncated)
    values so the payload location can be pinpointed at runtime.
    Returns the parsed RequestBody dict (POST body), or {} when the
    runbook was not triggered by a webhook."""
    # Diagnostic -- dump every candidate source with truncated values.
    candidates: dict = {"argv": [str(a)[:400] for a in sys.argv]}
    for k, v in os.environ.items():
        ku = k.upper()
        if any(t in ku for t in ("WEBHOOK", "AUTOMATION", "TRIGGER", "INPUT", "RUNBOOK", "JOB")):
            candidates[f"env:{k}"] = (v or "")[:400]
    # Try non-blocking stdin peek.
    try:
        import select as _select
        r, _, _ = _select.select([sys.stdin], [], [], 0.05)
        if r:
            stdin_data = sys.stdin.read(4000)
            candidates["stdin"] = stdin_data[:400]
    except Exception as _e:
        candidates["stdin_err"] = str(_e)[:200]
    _log("webhook_payload_discovery", **{k: v for k, v in list(candidates.items())[:20]})

    raw = os.environ.get("WEBHOOKDATA", "")
    if not raw and len(sys.argv) > 1 and sys.argv[1]:
        raw = sys.argv[1]
    # Broaden: any WEBHOOK*-suffixed var, or *_INPUT var.
    if not raw:
        for k, v in os.environ.items():
            if not v:
                continue
            if k.upper().startswith("WEBHOOK") or k.upper().endswith("_INPUT"):
                raw = v
                break
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        body = parsed.get("RequestBody", "") if isinstance(parsed, dict) else raw
        if isinstance(body, str) and body:
            body = json.loads(body)
        return body or {}
    except Exception:
        return {}


def _resolve_state_scope(raw):
    """Accept 'ALL' (str), ['ALL'], ['VT','DE'], etc. Always return a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        if raw.strip().startswith("["):
            try:
                v = json.loads(raw)
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        return [s.strip().upper() for s in raw.split(",") if s.strip()]
    return ["ALL"]


# ============================================================================
# Main
# ============================================================================
def main() -> int:
    invocation_mode = INVOCATION_MODE
    webhook_body = _parse_webhook_input()
    if webhook_body:
        invocation_mode = webhook_body.get("invocation_mode", "webhook")
    load_mode = webhook_body.get("load_mode", LOAD_MODE_DEFAULT) if webhook_body else LOAD_MODE_DEFAULT
    state_scope = _resolve_state_scope(
        webhook_body.get("state_scope") if webhook_body else STATE_SCOPE_DEFAULT
    )

    log("runbook_start",
        pipeline=PIPELINE_NAME,
        env=ENV_PREFIX,
        invocation_mode=invocation_mode,
        load_mode=load_mode,
        state_scope=state_scope)

    # Fresh run_id
    now = datetime.datetime.utcnow()
    run_id = f"prov-{now.strftime('%Y-%m-%dT%H-%M-%SZ')}-{uuid.uuid4().hex[:6]}"
    log("run_id_generated", run_id=run_id)

    # Config + manifest
    try:
        import pymongo  # provided by AA Python 3 package
        conn = _get_mongo_conn_string()
        log("mongo_secret_fetched", vault_uri=KEY_VAULT_URI)
        # Atlas SRV lookups fail from the Automation sandbox because its
        # resolver cannot answer _mongodb._tcp.<domain> SRV queries. Try
        # a direct connect first; on SRV failure, translate the URI to
        # its non-SRV equivalent via DNS-over-HTTPS and retry.
        try:
            mongo = pymongo.MongoClient(conn, serverSelectionTimeoutMS=15000)
            mongo.admin.command("ping")
        except Exception:
            direct = _srv_to_direct_mongo_uri(conn)
            log("mongo_srv_bypass_active", direct_hostcount=direct.count(",") + 1
                if direct.startswith("mongodb://") else 0)
            mongo = pymongo.MongoClient(direct, serverSelectionTimeoutMS=15000)
        config = _read_pipeline_config(mongo)
        log("config_read",
            config_keys=list(config.keys()),
            found=bool(config))
        _write_run_manifest(mongo, run_id, load_mode, state_scope,
                            invocation_mode, config)
        log("run_manifest_written", run_id=run_id)
    except Exception as exc:
        log("mongo_step_failed",
            error_type=type(exc).__name__,
            error_msg=str(exc),
            traceback=traceback.format_exc()[-2000:])
        # Do NOT abort -- LLD §2.1 tolerates first-run when config is empty.
        # But we MUST still start Control so the operator sees a run happen.

    # Start Control
    try:
        result = _start_control_job(run_id, load_mode, state_scope)
        log("control_job_started",
            run_id=run_id,
            name=result.get("name"),
            status=result.get("properties", {}).get("status"))
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:1500]
        except Exception:
            pass
        log("control_job_start_failed",
            run_id=run_id,
            http_code=exc.code,
            reason=exc.reason,
            body=body)
        return 1
    except Exception as exc:
        log("control_job_start_failed",
            run_id=run_id,
            error_type=type(exc).__name__,
            error_msg=str(exc),
            traceback=traceback.format_exc()[-2000:])
        return 1

    log("runbook_exit", run_id=run_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
