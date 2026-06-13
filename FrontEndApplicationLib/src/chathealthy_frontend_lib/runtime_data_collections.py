"""Runtime data collection bindings driven by ChatHealthyConfig.DBVersions.

Each front-end runtime that consumes versioned data calls
`bind_from_manifest()` at FastAPI startup. The function:

  1. Reads target_id and env from build_info.json baked into the build.
  2. Reads ChatHealthyConfig.DBVersions.find_one({"env": env}) on the
     ChatHealthyFrontEnd cluster via MONGO_FRONTEND_connectionString.
  3. Locates the targets[] entry whose deployment_target == target_id.
  4. Binds module-level statics for each collection_environment_name to
     its runtime_collection_name.

Accessors providers_coll() and specialty_meta_coll() return the bound
pymongo Collection object. They raise if the binding is missing — no
silent fallback to env-prefixed paths.

The router exposes POST /admin/swap to rebind the statics in place
(activation runbook calls this), and GET /debug/active_collections to
report the in-memory bindings vs the current doc-resolved bindings.

Per EPIC-010-F-101-S-005 (Data version management).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Header
from pymongo import MongoClient
from pymongo.collection import Collection

from .mongo_utilities import ChatHealthyMongoUtilities


_CONFIG_DB = "ChatHealthyConfig"
_CONFIG_COLL = "DBVersions"

_PROVIDER_SLOT = "PROVIDER_COLLECTION"
_SPECIALTY_META_SLOT = "SPECIALTY_META_COLLECTION"


class _State:
    target_id: str | None = None
    env: str | None = None
    bindings: dict[str, str] = {}


_state = _State()


def _read_build_info() -> dict:
    candidates = [
        Path("build_info.json"),
        Path("/app/build_info.json"),
        Path(__file__).resolve().parents[3] / "build_info.json",
    ]
    for path in candidates:
        if path.is_file():
            return json.loads(path.read_text(encoding="utf-8"))
    raise RuntimeError(
        "runtime_data_collections: build_info.json not found in "
        f"{[str(p) for p in candidates]}. The build must write it so "
        "the runtime can identify its target_id and env."
    )


def _mongo_client() -> MongoClient:
    return ChatHealthyMongoUtilities().getConnection()


def _read_env_doc(env: str) -> dict:
    coll = _mongo_client()[_CONFIG_DB][_CONFIG_COLL]
    doc = coll.find_one({"env": env})
    if doc is None:
        raise RuntimeError(
            f"runtime_data_collections: ChatHealthyConfig.DBVersions has no document "
            f"for env={env!r}. Seed the document before starting the runtime "
            "(EPIC-010-F-101-S-005-REQ-B-003)."
        )
    return doc


def _bindings_from_doc(doc: dict, target_id: str) -> dict[str, str]:
    for entry in doc.get("targets", []):
        if entry.get("deployment_target") != target_id:
            continue
        return {
            c["collection_environment_name"]: c["runtime_collection_name"]
            for c in entry.get("collections", [])
        }
    raise RuntimeError(
        f"runtime_data_collections: ChatHealthyConfig.DBVersions env={doc.get('env')!r} "
        f"has no targets[] entry for deployment_target={target_id!r}. "
        "Update the env doc to include this runtime."
    )


def bind_from_manifest() -> None:
    """Read target_id (from build_info.json) + env (from ENV_PREFIX env
    var) and bind module-level collection statics from
    ChatHealthyConfig.DBVersions. Call from the runtime's startup hook.
    Raises on any missing piece — no silent fallbacks.

    target_id is build-time-stable (baked into the image); env is
    deploy-time-stable (set per HF Space deploy via ENV_PREFIX), so the
    two come from different sources by design.
    """
    info = _read_build_info()
    target_id = info.get("target_id")
    env = os.environ.get("ENV_PREFIX")
    if not target_id:
        raise RuntimeError(
            f"runtime_data_collections: build_info.json missing target_id "
            f"({target_id!r}). Build step must emit it."
        )
    if not env:
        raise RuntimeError(
            "runtime_data_collections: ENV_PREFIX env var not set. The HF "
            "deploy must set it per target environment binding."
        )
    doc = _read_env_doc(env)
    bindings = _bindings_from_doc(doc, target_id)
    _state.target_id = target_id
    _state.env = env
    _state.bindings = bindings


def _coll_for(slot: str) -> Collection:
    fqn = _state.bindings.get(slot)
    if not fqn:
        raise RuntimeError(
            f"runtime_data_collections: slot {slot!r} is not bound. Call "
            "bind_from_manifest() at startup."
        )
    db_name, coll_name = fqn.split(".", 1)
    return _mongo_client()[db_name][coll_name]


def providers_coll() -> Collection:
    return _coll_for(_PROVIDER_SLOT)


def specialty_meta_coll() -> Collection:
    return _coll_for(_SPECIALTY_META_SLOT)


# ── Admin / debug endpoints ────────────────────────────────────────────

router = APIRouter()


def _require_bearer(authorization: str | None = Header(default=None)) -> str:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Bearer token required.")
    token = authorization[7:].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Empty bearer token.")
    token_map = json.loads(os.environ.get("API_TOKEN_MAP", "{}"))
    user = token_map.get(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid bearer token.")
    return user


@router.post("/admin/swap", status_code=202)
def admin_swap(
    payload: dict[str, Any],
    _user: str = Depends(_require_bearer),
) -> dict[str, Any]:
    """Rebind module-level statics from the posted collections map.

    Expected payload shape:
        { "collections": [
            { "collection_environment_name": "...", "runtime_collection_name": "..." },
            ...
        ] }

    The activation runbook (ChangeDBVersion) reads each env doc and POSTs
    to this endpoint per target. The previous bindings are discarded; the
    new map is the in-memory truth until the next swap or restart.
    """
    items = payload.get("collections")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="collections[] required.")
    new_bindings: dict[str, str] = {}
    for entry in items:
        slot = entry.get("collection_environment_name")
        fqn = entry.get("runtime_collection_name")
        if not slot or not fqn:
            raise HTTPException(status_code=400, detail="each collections[] entry needs both names.")
        new_bindings[slot] = fqn
    _state.bindings = new_bindings
    return {"status": "ok", "target_id": _state.target_id, "env": _state.env, "bindings": new_bindings}


@router.get("/debug/active_collections")
def debug_active_collections(
    _user: str = Depends(_require_bearer),
) -> dict[str, Any]:
    """Report the in-memory bindings vs the bindings currently in the
    env's ChatHealthyConfig.DBVersions document. state is 'stable' when they match,
    'drift' when they do not — usually because an activation push failed
    and the runtime is still on the old map.
    """
    in_memory = dict(_state.bindings)
    doc_resolved: dict[str, str] = {}
    state = "stable"
    try:
        doc = _read_env_doc(_state.env) if _state.env else None
        if doc and _state.target_id:
            doc_resolved = _bindings_from_doc(doc, _state.target_id)
    except Exception as exc:
        return {
            "target_id": _state.target_id,
            "env": _state.env,
            "in_memory": in_memory,
            "doc_resolved": None,
            "state": "doc_unreachable",
            "error": str(exc),
        }
    if doc_resolved != in_memory:
        state = "drift"
    return {
        "target_id": _state.target_id,
        "env": _state.env,
        "in_memory": in_memory,
        "doc_resolved": doc_resolved,
        "state": state,
    }
