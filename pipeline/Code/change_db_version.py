"""change_db_version.py - ChangeDBVersion activation runbook.

Published to Automation Account ChatHealthyJobManager as runbook name
'ChangeDBVersion'. Triggered by the gateway when ChatHealthyTask =
'ChangeDBVersion' is posted to POST /Router.

Reads every document from ChatHealthyConfig.DBVersions on the ChatHealthyFrontEnd
cluster. For each env doc, walks targets[] and POSTs the target's
collections map to the runtime's /admin/swap endpoint. The runtime URL
comes from change_db_version_target_url_registry.json baked next to
this runbook at build time (sourced from
deployment_architecture.json.environments[].node_address). The
deployment-architecture file is NOT read at runtime.

Per EPIC-010-F-101-S-005 (Data version management) REQ-B-004:
ChatHealthyDataPipelinesGatewayFunctionApp is the facade; this runbook
on ChatHealthyJobManager is what spawns from it to propagate.

Input payload (JSON-encoded `payload` parameter, read via sys.argv[1]):
    { "job_id": "<gateway-minted>" }

Environment (Automation Variables):
    MONGO_FRONTEND_connectionString - front-end cluster (read ChatHealthyConfig.DBVersions).
    API_TOKEN_MAP                   - JSON map; this runbook uses any token from
                                      it as the Bearer credential for /admin/swap.
"""
from __future__ import annotations
from chathealthy_frontend_lib.logging_service import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

# When this module runs as an Azure Automation runbook the deploy step
# has pushed every secrets[] entry into the AA as an Automation Variable,
# but AA does NOT auto-inject those into os.environ. Copy them in
# explicitly using the AA-only `automationassets` module before any
# os.environ.get() reads. Same pattern the CHDM orchestrator runbook
# uses. When running locally (no AA) the import fails harmlessly and
# os.environ reflects the operator's normal shell env.
try:
    import automationassets  # type: ignore[import-not-found]
    for _k in (
        "API_TOKEN_MAP",
        "MONGO_FRONTEND_connectionString",
    ):
        try:
            os.environ[_k] = str(automationassets.get_automation_variable(_k))
        except Exception:
            pass
except ImportError:
    pass

# CHLS Mongo destination + SRV bypass. Every runbook logs to
# Pipelines.Log_<env>; this configuration makes the ChatHealthyLoggingService
# singleton (line ~108) wire the Mongo handler on first log call.
os.environ.setdefault("CH_SPACE_NAME", "change-db-version")
os.environ.setdefault("CH_COMPONENT", "change-db-version")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
try:
    from chathealthy_frontend_lib.pipeline_boot import srv_to_direct_uri as _srv_to_direct
    _mongo_uri = os.environ.get("MONGO_FRONTEND_connectionString", "")
    if _mongo_uri.startswith("mongodb+srv://"):
        os.environ["MONGO_FRONTEND_connectionString"] = _srv_to_direct(_mongo_uri)
except Exception as _bootstrap_exc:  # noqa: BLE001
    sys.stderr.write(
        f"change_db_version: SRV bypass failed "
        f"({type(_bootstrap_exc).__name__}: {_bootstrap_exc}); "
        "falling back to stderr-only logging.\n"
    )
    os.environ["CH_LOG_DESTINATION"] = "stderr"

# Atlas SRV connection strings (mongodb+srv://...) require dnspython
# SRV resolution. The AA Python3 sandbox's system DNS does not answer
# external SRV queries reliably, so explicitly point dnspython at
# public resolvers. This is harmless in local/dev contexts (overrides
# only the in-process resolver). Must run BEFORE pymongo is imported.
try:
    import dns.resolver  # type: ignore[import-not-found]
    _r = dns.resolver.Resolver(configure=False)
    _r.nameservers = ["8.8.8.8", "8.8.4.4", "1.1.1.1", "1.0.0.1"]
    _r.timeout = 5
    _r.lifetime = 10
    dns.resolver.default_resolver = _r
except ImportError:
    pass

from pymongo import MongoClient

# Atlas requires a current CA bundle to validate its TLS chain. The AA
# sandbox Windows Python doesn't always have a current trust store, so
# we explicitly point pymongo at certifi's bundled CA file. Falls back
# silently when certifi isn't installed (local dev / non-AA contexts
# where the system trust store is usually fine).
try:
    import certifi  # type: ignore[import-not-found]
    _MONGO_TLS_CA_FILE: str | None = certifi.where()
    # urllib.request.urlopen uses Python's default ssl context, which
    # on the AA Windows sandbox has the same stale trust store that
    # broke the Atlas connection. Build a context off certifi so the
    # /admin/swap POSTs validate against a current Mozilla CA bundle.
    _SWAP_SSL_CONTEXT: ssl.SSLContext | None = ssl.create_default_context(cafile=_MONGO_TLS_CA_FILE)
except ImportError:
    _MONGO_TLS_CA_FILE = None
    _SWAP_SSL_CONTEXT = None


_REGISTRY_FILENAME = "change_db_version_target_url_registry.json"
_CONFIG_DB = "ChatHealthyConfig"
_CONFIG_COLL = "DBVersions"

# Placeholder. The build step for target_azure_automation_runbook_
# change_db_version replaces the assignment on the next line with the
# actual {env: {target_id: node_address}} map derived from
# deployment_architecture.json. Text-level replacement; assignment
# must stay on a single line and the exact assignment string must
# appear nowhere else in this file (not even in this comment).
_BAKED_REGISTRY: dict = {}


def _log(msg: str) -> None:
    ChatHealthyLoggingService().info(f"[ChangeDBVersion] {msg}")


def _read_registry() -> dict[str, str]:
    if _BAKED_REGISTRY:
        return _BAKED_REGISTRY
    here = Path(__file__).resolve().parent
    registry_path = here / _REGISTRY_FILENAME
    if not registry_path.is_file():
        sys.exit(
            f"ERROR: target URL registry not found at {registry_path} and "
            "_BAKED_REGISTRY is empty. The build step for the azure_"
            "automation_runbook target must either populate _BAKED_REGISTRY "
            "or write the sibling registry JSON file from deployment_"
            "architecture.json."
        )
    return json.loads(registry_path.read_text(encoding="utf-8"))


def _bearer_token() -> str:
    raw = os.environ.get("API_TOKEN_MAP", "{}")
    token_map = json.loads(raw)
    if not token_map:
        sys.exit("ERROR: API_TOKEN_MAP is empty; cannot authenticate to runtime endpoints.")
    return next(iter(token_map))


def _post_swap(target_url: str, collections: list[dict], token: str, timeout: int = 30) -> tuple[int, str]:
    url = target_url.rstrip("/") + "/admin/swap"
    body = json.dumps({"collections": collections}).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # Cloudflare's bot firewall (which fronts dev/qa/prod.
            # chathealthy.ai) rejects requests with the default
            # Python-urllib User-Agent with code 1010. Present a
            # standard browser UA so the swap call gets through.
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=_SWAP_SSL_CONTEXT) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, str(exc)


def _read_webhook_payload() -> dict:
    """Unwrap legacy Azure Automation's WebhookData envelope into the
    inner JSON payload. AA mangles the wrapper into a non-JSON format
    with unquoted keys + shell-splits across sys.argv. Rejoin with
    spaces, locate 'RequestBody:', raw_decode the embedded JSON object.
    Same approach as runbooks/data_migrator/orchestrator.py::_read_payload."""
    if len(sys.argv) < 2:
        raise ChatHealthyException(mode="runtime_error", message="no payload: sys.argv[1] missing")
    s = " ".join(sys.argv[1:])
    marker = "RequestBody:"
    idx = s.find(marker)
    if idx == -1:
        raise ChatHealthyException(mode="runtime_error", message=f"ChangeDBVersion: sys.argv missing 'RequestBody:' marker; "
            f"argv_count={len(sys.argv)}; joined first 500 chars={s[:500]!r}"
        )
    rest = s[idx + len(marker):].lstrip()
    try:
        body, _consumed = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError as e:
        raise ChatHealthyException(mode="runtime_error", message=f"ChangeDBVersion: RequestBody not parseable JSON; "
            f"rest first 500 chars={rest[:500]!r}; error={e}")
    if not isinstance(body, dict):
        raise ChatHealthyException(mode="runtime_error", message=f"ChangeDBVersion: RequestBody is not a dict; got {type(body).__name__}"
        )
    return body


def main() -> int:
    payload = _read_webhook_payload()
    job_id = payload.get("job_id") or "ChangeDBVersion-no-job-id"
    env = payload.get("env")
    if not env:
        sys.exit("ERROR: payload missing required field 'env'.")
    _log(f"job_id={job_id} env={env}")

    registry = _read_registry()
    _log(f"loaded target URL registry: {len(registry)} target(s)")
    token = _bearer_token()

    uri = os.environ.get("MONGO_FRONTEND_connectionString")
    if not uri:
        sys.exit("ERROR: MONGO_FRONTEND_connectionString not set.")
    from chathealthy_frontend_lib.mongo_utilities import ChatHealthyMongoUtilities
    client = ChatHealthyMongoUtilities("MONGO_FRONTEND_connectionString").getConnection()
    doc = client[_CONFIG_DB][_CONFIG_COLL].find_one({"env": env})
    client.close()
    if doc is None:
        sys.exit(f"ERROR: no ChatHealthyConfig.DBVersions doc for env={env!r}.")
    _log(f"loaded env doc for {env!r} from ChatHealthyConfig.DBVersions")

    has_exception = False
    for entry in doc.get("targets", []):
        target_id = entry.get("deployment_target")
        collections = entry.get("collections", [])
        url = registry.get(env, {}).get(target_id) if isinstance(registry.get(env), dict) else registry.get(target_id)
        if not url:
            has_exception = True
            _log(f"  {env}/{target_id}: NO URL in registry")
            continue
        code, body = _post_swap(url, collections, token)
        if code == 202:
            _log(f"  {env}/{target_id}: 202 Accepted")
        else:
            has_exception = True
            _log(f"  {env}/{target_id}: {code} {body[:200]}")

    return 1 if has_exception else 0


if __name__ == "__main__":
    sys.exit(main())
