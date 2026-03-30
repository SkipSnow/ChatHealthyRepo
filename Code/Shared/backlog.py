"""
Backlog — ChatHealthy Product Backlog (MS DevOps equivalent).

Independent of brain_loop.py operational assignments.

Database: {ENV_PREFIX}_Backlog
Collection: features

Stories are children embedded inside their parent feature document.

Feature schema:
  feature_id    str        FEAT-XXXXXX
  title         str
  description   str
  status        str        backlog | in_progress | done | cancelled
  components    list[str]  e.g. ["backend/FindCareChat", "frontend/ChatWindow"]
  priority      str        critical | high | medium | low
  created_at    str        ISO
  updated_at    str        ISO
  stories       list[Story]

Story schema (embedded in feature.stories):
  story_id                str   STORY-XXXXXX
  title                   str
  description             str
  status                  str   backlog | in_progress | done | cancelled
  components              list[str]
  acceptance_criteria     list[str]
  priority                str
  brain_assignment_id     str | None  links to brain_loop assignment
  created_at              str
  updated_at              str
"""

import os
import uuid
from datetime import datetime, timezone
from typing import Optional

from pymongo import MongoClient

_ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")
_BACKLOG_DB = f"{_ENV_PREFIX}_Backlog"
# Backlog lives on the always-on FrontEnd cluster, not the DataPipelines cluster
_MONGO_URI  = os.getenv("MONGO_FRONTEND_connectionString", os.getenv("MONGO_connectionString", ""))

_client_cache: dict = {}


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def _db():
    if "client" not in _client_cache:
        if not _MONGO_URI:
            raise RuntimeError("MONGO_connectionString not set.")
        _client_cache["client"] = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=5000)
    return _client_cache["client"][_BACKLOG_DB]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _feat_id() -> str:
    return f"FEAT-{uuid.uuid4().hex[:6].upper()}"


def _story_id() -> str:
    return f"STORY-{uuid.uuid4().hex[:6].upper()}"


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------

def create_feature(
    title: str,
    description: str = "",
    components: list = None,
    priority: str = "medium",
    status: str = "backlog",
    feature_id: str = None,
) -> str:
    """Create a new feature with an empty stories list. Returns feature_id."""
    fid = feature_id or _feat_id()
    doc = {
        "feature_id":  fid,
        "title":       title,
        "description": description,
        "status":      status,
        "components":  components or [],
        "priority":    priority,
        "stories":     [],
        "created_at":  _now(),
        "updated_at":  _now(),
    }
    _db()["features"].replace_one({"feature_id": fid}, doc, upsert=True)
    print(f"[Backlog] Feature: {fid} — {title}")
    return fid


def update_feature(feature_id: str, **fields) -> None:
    """Update fields on a feature (not stories)."""
    fields["updated_at"] = _now()
    _db()["features"].update_one({"feature_id": feature_id}, {"$set": fields})


def get_feature(feature_id: str) -> Optional[dict]:
    """Return a feature with all its embedded stories."""
    return _db()["features"].find_one({"feature_id": feature_id}, {"_id": 0})


def list_features(status: str = None, component: str = None) -> list:
    """List features, optionally filtered by status or component."""
    q = {}
    if status:
        q["status"] = status
    if component:
        q["components"] = component
    priority_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    results = list(_db()["features"].find(q, {"_id": 0}))
    return sorted(results, key=lambda f: priority_order.get(f.get("priority", "medium"), 2))


# ---------------------------------------------------------------------------
# Stories — embedded in their parent feature
# ---------------------------------------------------------------------------

def add_story(
    feature_id: str,
    title: str,
    description: str = "",
    components: list = None,
    acceptance_criteria: list = None,
    priority: str = "medium",
    status: str = "backlog",
    brain_assignment_id: str = None,
    story_id: str = None,
) -> str:
    """Add a story as a child of a feature. Returns story_id."""
    sid = story_id or _story_id()
    story = {
        "story_id":             sid,
        "title":                title,
        "description":          description,
        "status":               status,
        "components":           components or [],
        "acceptance_criteria":  acceptance_criteria or [],
        "priority":             priority,
        "brain_assignment_id":  brain_assignment_id,
        "created_at":           _now(),
        "updated_at":           _now(),
    }
    _db()["features"].update_one(
        {"feature_id": feature_id},
        {"$push": {"stories": story}, "$set": {"updated_at": _now()}},
    )
    print(f"[Backlog] Story: {sid} — {title} (under {feature_id})")
    return sid


def update_story(feature_id: str, story_id: str, **fields) -> None:
    """Update fields on an embedded story."""
    fields["updated_at"] = _now()
    set_fields = {f"stories.$.{k}": v for k, v in fields.items()}
    _db()["features"].update_one(
        {"feature_id": feature_id, "stories.story_id": story_id},
        {"$set": set_fields},
    )


# ---------------------------------------------------------------------------
# Backlog view
# ---------------------------------------------------------------------------

def list_backlog(status: str = None) -> list:
    """Return all features (with embedded stories) filtered by status."""
    return list_features(status=status)
