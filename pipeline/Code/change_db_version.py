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
    MONGO_connectionString          - pipeline cluster (write status doc).
    API_TOKEN_MAP                   - JSON map; this runbook uses any token from
                                      it as the Bearer credential for /admin/swap.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from pymongo import MongoClient


_REGISTRY_FILENAME = "change_db_version_target_url_registry.json"
_STATUS_COLLECTION = ("admin", "ChangeDBVersion_jobs")
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
    print(f"[ChangeDBVersion] {msg}", flush=True)


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
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        return exc.code, body
    except urllib.error.URLError as exc:
        return 0, str(exc)


def _init_status_doc(job_id: str, total_targets: int) -> None:
    uri = os.environ.get("MONGO_connectionString")
    if not uri:
        _log("WARN: MONGO_connectionString not set; status doc will not be written.")
        return
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db, coll = _STATUS_COLLECTION
    client[db][coll].insert_one({
        "job_id": job_id,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "total_targets": total_targets,
        "results": [],
        "has_exception": False,
    })
    client.close()


def _append_result(job_id: str, result: dict) -> None:
    uri = os.environ.get("MONGO_connectionString")
    if not uri:
        return
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db, coll = _STATUS_COLLECTION
    client[db][coll].update_one({"job_id": job_id}, {"$push": {"results": result}})
    client.close()


def _finalize_status(job_id: str, *, has_exception: bool) -> None:
    uri = os.environ.get("MONGO_connectionString")
    if not uri:
        return
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    db, coll = _STATUS_COLLECTION
    client[db][coll].update_one(
        {"job_id": job_id},
        {"$set": {
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "has_exception": has_exception,
        }},
    )
    client.close()


def _read_webhook_payload() -> dict:
    """Unwrap legacy Azure Automation's WebhookData envelope into the
    inner JSON payload. AA mangles the wrapper into a non-JSON format
    with unquoted keys + shell-splits across sys.argv. Rejoin with
    spaces, locate 'RequestBody:', raw_decode the embedded JSON object.
    Same approach as runbooks/data_migrator/orchestrator.py::_read_payload."""
    if len(sys.argv) < 2:
        raise RuntimeError("no payload: sys.argv[1] missing")
    s = " ".join(sys.argv[1:])
    marker = "RequestBody:"
    idx = s.find(marker)
    if idx == -1:
        raise RuntimeError(
            f"ChangeDBVersion: sys.argv missing 'RequestBody:' marker; "
            f"argv_count={len(sys.argv)}; joined first 500 chars={s[:500]!r}"
        )
    rest = s[idx + len(marker):].lstrip()
    try:
        body, _consumed = json.JSONDecoder().raw_decode(rest)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"ChangeDBVersion: RequestBody not parseable JSON; "
            f"rest first 500 chars={rest[:500]!r}; error={e}"
        )
    if not isinstance(body, dict):
        raise RuntimeError(
            f"ChangeDBVersion: RequestBody is not a dict; got {type(body).__name__}"
        )
    return body


def main() -> int:
    payload = _read_webhook_payload()
    job_id = payload.get("job_id") or "ChangeDBVersion-no-job-id"
    _log(f"job_id={job_id}")

    registry = _read_registry()
    _log(f"loaded target URL registry: {len(registry)} target(s)")
    token = _bearer_token()

    uri = os.environ.get("MONGO_FRONTEND_connectionString")
    if not uri:
        sys.exit("ERROR: MONGO_FRONTEND_connectionString not set.")
    client = MongoClient(uri, serverSelectionTimeoutMS=10000)
    docs = list(client[_CONFIG_DB][_CONFIG_COLL].find({}))
    client.close()
    _log(f"loaded {len(docs)} env doc(s) from ChatHealthyConfig.DBVersions")

    total_targets = sum(len(d.get("targets", [])) for d in docs)
    _init_status_doc(job_id, total_targets)

    has_exception = False
    for doc in docs:
        env = doc.get("env")
        for entry in doc.get("targets", []):
            target_id = entry.get("deployment_target")
            collections = entry.get("collections", [])
            url = registry.get(env, {}).get(target_id) if isinstance(registry.get(env), dict) else registry.get(target_id)
            result: dict = {"env": env, "target_id": target_id, "collection_count": len(collections)}
            if not url:
                result["status"] = "no_url"
                result["detail"] = f"registry has no URL for env={env!r} target_id={target_id!r}"
                has_exception = True
                _log(f"  {env}/{target_id}: NO URL in registry")
                _append_result(job_id, result)
                continue
            code, body = _post_swap(url, collections, token)
            result["http_status"] = code
            result["response_preview"] = body[:300]
            if code == 200:
                result["status"] = "ok"
                _log(f"  {env}/{target_id}: 200 OK")
            else:
                result["status"] = "fail"
                has_exception = True
                _log(f"  {env}/{target_id}: {code} {body[:200]}")
            _append_result(job_id, result)

    _finalize_status(job_id, has_exception=has_exception)
    return 1 if has_exception else 0


if __name__ == "__main__":
    sys.exit(main())
