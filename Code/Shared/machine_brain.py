"""
MachineBrain — Persistent architectural memory for ChatHealthy.

framework_02 enforcement:
  - Query before implementing: get_decisions(topic)
  - Write after deciding:      store_decision(decision)

Database: {ENV_PREFIX}_MachineBrain on ChatHealthyFrontEndCluster
Collection: decisions

ADR: ADR-0007
"""

import os
from datetime import datetime, timezone
from typing import Optional
from pymongo import MongoClient, ASCENDING
from pymongo.errors import PyMongoError


ENV_PREFIX = os.getenv("ENV_PREFIX", "dev")
_DB_NAME = f"{ENV_PREFIX}_MachineBrain"
_COLLECTION = "decisions"

_client: Optional[MongoClient] = None


def _get_collection():
    global _client
    if _client is None:
        conn = os.getenv("MONGODB_CONNECTION_STRING")
        if not conn:
            raise EnvironmentError("MONGODB_CONNECTION_STRING is not set.")
        _client = MongoClient(conn)
        _client.admin.command("ping")
        # Ensure index on topic for fast retrieval
        _client[_DB_NAME][_COLLECTION].create_index([("topic", ASCENDING)])
        _client[_DB_NAME][_COLLECTION].create_index([("adr_id", ASCENDING)])
        _client[_DB_NAME][_COLLECTION].create_index([("components", ASCENDING)])
    return _client[_DB_NAME][_COLLECTION]


def get_decisions(topic: str) -> list[dict]:
    """
    Query Machine Brain for decisions related to a topic.
    Must be called before any non-trivial implementation.

    Args:
        topic: keyword or phrase describing the implementation area

    Returns:
        List of matching decision records, most recent first.
    """
    try:
        col = _get_collection()
        results = list(
            col.find(
                {"$or": [
                    {"topic": {"$regex": topic, "$options": "i"}},
                    {"components": {"$regex": topic, "$options": "i"}},
                    {"decision": {"$regex": topic, "$options": "i"}},
                ]},
                {"_id": 0}
            ).sort("created_at", -1)
        )
        return results
    except PyMongoError as e:
        print(f"[MachineBrain] WARNING: get_decisions failed — {e}", flush=True)
        return []


def store_decision(
    topic: str,
    decision: str,
    rationale: str,
    risk: str,
    created_by: str,
    adr_id: Optional[str] = None,
    constraints: Optional[list[str]] = None,
    components: Optional[list[str]] = None,
    decision_type: str = "architectural",
) -> bool:
    """
    Store a decision in Machine Brain after it has been made.
    Must be called after any architectural or significant operational decision.

    Args:
        topic:        Short topic name
        decision:     The decision made
        rationale:    Why this decision was made
        risk:         Low | Moderate | High | Critical | Suicidal
        created_by:   GPT | Claude | Skip
        adr_id:       ADR reference (e.g. ADR-0007)
        constraints:  List of constraints this decision imposes
        components:   List of system components affected
        decision_type: architectural | operational | security | compliance

    Returns:
        True if stored successfully, False otherwise.
    """
    valid_risks = {"Low", "Moderate", "High", "Critical", "Suicidal"}
    if risk not in valid_risks:
        raise ValueError(f"risk must be one of {valid_risks}")

    record = {
        "adr_id": adr_id,
        "topic": topic,
        "type": decision_type,
        "decision": decision,
        "rationale": rationale,
        "risk": risk,
        "constraints": constraints or [],
        "components": components or [],
        "framework": "framework_02",
        "created_by": created_by,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "version": 1,
    }

    try:
        col = _get_collection()
        col.insert_one(record)
        return True
    except PyMongoError as e:
        print(f"[MachineBrain] WARNING: store_decision failed — {e}", flush=True)
        return False


def list_all_decisions() -> list[dict]:
    """Return all decisions, sorted by created_at descending."""
    try:
        col = _get_collection()
        return list(col.find({}, {"_id": 0}).sort("created_at", -1))
    except PyMongoError as e:
        print(f"[MachineBrain] WARNING: list_all_decisions failed — {e}", flush=True)
        return []
