# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""AA runbook Mongo-logging bootstrap.

Every AA runbook that logs via ChatHealthyLoggingService with Mongo
destination must have MONGO_FRONTEND_connectionString + CH_SPACE_NAME +
ENV_PREFIX set BEFORE its first log() call. Otherwise CHLS's
_build_mongo_handler raises 'mongo_log_handler_env_unset' and the
runbook aborts before any structured event lands.

Additionally, AA Python 3 sandbox cannot resolve _mongodb._tcp.<host>
SRV records, so the mongodb+srv:// URI from KV must be pre-converted
to its non-SRV mongodb:// form via DNS-over-HTTPS before CHLS
constructs a MongoClient with it.

Usage from a runbook:

    from chathealthy_frontend_lib.pipeline_boot import bootstrap_aa_mongo_logging

    def main() -> int:
        bootstrap_aa_mongo_logging(
            kv_uri="https://kv-chpipeline-dev.vault.azure.net/",
            secret_name="MONGO-FRONTEND-connectionString",
            component_name="watchdog",
        )
        # ... rest of main; log() calls now write to Log_<env> Mongo.

This module is inlined into every AA runbook by the build chain
(build_chathealthy.py _inline_chathealthy_lib_if_used), so runbooks
never need to install any package beyond the AA sandbox baseline.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request

try:
    import certifi as _certifi
    _CA_BUNDLE = _certifi.where()
except ImportError:
    _CA_BUNDLE = None


def _mi_token(resource: str) -> str:
    """Fetch a bearer token from the ambient managed identity.

    Prefers the AA sandbox IDENTITY_ENDPOINT / IDENTITY_HEADER protocol
    when those are present (App Service style); falls back to IMDS
    (VM style) otherwise. AZURE_CLIENT_ID selects a specific user-
    assigned MI when the identity chain has more than one.
    """
    identity_endpoint = os.environ.get("IDENTITY_ENDPOINT", "").strip()
    identity_header = os.environ.get("IDENTITY_HEADER", "").strip()
    cid = os.environ.get("AZURE_CLIENT_ID", "").strip()
    if identity_endpoint and identity_header:
        q = f"?api-version=2019-08-01&resource={resource}"
        if cid:
            q += f"&client_id={cid}"
        req = urllib.request.Request(
            f"{identity_endpoint}{q}",
            headers={"X-IDENTITY-HEADER": identity_header},
        )
    else:
        q = f"?api-version=2018-02-01&resource={resource}"
        if cid:
            q += f"&client_id={cid}"
        req = urllib.request.Request(
            f"http://169.254.169.254/metadata/identity/oauth2/token{q}",
            headers={"Metadata": "true"},
        )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["access_token"]


def _kv_secret(kv_uri: str, secret_name: str) -> str:
    """Fetch a secret value from Azure Key Vault via the MI token."""
    tok = _mi_token("https://vault.azure.net")
    url = (
        f"{kv_uri.rstrip('/')}/secrets/{secret_name}?api-version=7.4"
    )
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {tok}"}
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))["value"]


def _doh_query(name: str, rtype: str) -> list:
    """DNS-over-HTTPS query via Cloudflare 1.1.1.1."""
    type_num = {"SRV": 33, "TXT": 16}[rtype]
    url = (
        f"https://1.1.1.1/dns-query?name={name}&type={type_num}"
    )
    ctx = (
        ssl.create_default_context(cafile=_CA_BUNDLE)
        if _CA_BUNDLE
        else ssl.create_default_context()
    )
    req = urllib.request.Request(
        url, headers={"accept": "application/dns-json"}
    )
    with urllib.request.urlopen(req, context=ctx, timeout=15) as r:
        return json.loads(r.read().decode("utf-8")).get("Answer") or []


def srv_to_direct_uri(srv_uri: str) -> str:
    """Translate mongodb+srv:// URI to its non-SRV mongodb:// equivalent.

    Non-SRV inputs pass through unchanged so callers can call this
    unconditionally.
    """
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
    hosts: list[str] = []
    for a in _doh_query(f"_mongodb._tcp.{host_only}", "SRV"):
        if a.get("type") != 33:
            continue
        parts = a.get("data", "").strip().split()
        if len(parts) >= 4:
            hosts.append(f"{parts[3].rstrip('.')}:{parts[2]}")
    if not hosts:
        from chathealthy_frontend_lib.exceptions import ChatHealthyException  # noqa: PLC0415
        raise ChatHealthyException(
            mode="doh_srv_lookup_no_hosts",
            message=f"DoH SRV lookup returned no hosts for {host_only}",
            component="pipeline_boot.srv_to_direct_uri",
            host=host_only,
        )
    txt_opts = ""
    for a in _doh_query(host_only, "TXT"):
        if a.get("type") != 16:
            continue
        val = a.get("data", "").strip().strip('"')
        if val:
            txt_opts = val
            break
    base = "mongodb://" + userinfo + ",".join(hosts) + trailing
    if txt_opts:
        sep = "&" if "?" in base else "?"
        base = base + sep + txt_opts
    if "tls=" not in base and "ssl=" not in base:
        sep = "&" if "?" in base else "?"
        base = base + sep + "tls=true"
    return base


def bootstrap_aa_mongo_logging(
    kv_uri: str,
    secret_name: str,
    component_name: str,
    env_prefix: str | None = None,
) -> None:
    """Fetch Mongo URI from KV, SRV-bypass it, set env for CHLS.

    MUST be called BEFORE the runbook's first log() call. Sets:
      * MONGO_FRONTEND_connectionString (direct-form URI)
      * CH_SPACE_NAME (component_name)
      * ENV_PREFIX (arg or AUTOMATION_ENV_PREFIX or 'dev')
      * CH_COMPONENT (component_name)
      * CH_LOG_DESTINATION ('stderr,mongo' if not already set)
    """
    resolved_env = (
        env_prefix
        or os.environ.get("AUTOMATION_ENV_PREFIX")
        or "dev"
    )
    os.environ["MONGO_FRONTEND_connectionString"] = srv_to_direct_uri(
        _kv_secret(kv_uri, secret_name)
    )
    os.environ.setdefault("CH_SPACE_NAME", component_name)
    os.environ.setdefault("ENV_PREFIX", resolved_env)
    os.environ.setdefault("CH_COMPONENT", component_name)
    os.environ.setdefault("CH_LOG_DESTINATION", "stderr,mongo")
