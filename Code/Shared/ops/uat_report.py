# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# uat_report.py — DevOps tool: generates clean UAT welcome report.
#
# Starts blank every session. Reads features from brain/uat config.
# Build number comes from /health endpoint at runtime.
# Done, Bugs Fixed, New Features columns are ALWAYS blank at start.
#
# Usage: imported by main.py, called to build the welcome message.
# Location: Code/Shared/ops/ (DevOps tool, not business logic)

import json
import logging
import os
from pathlib import Path

_log = logging.getLogger("findcare.uat_report")

# UAT feature definitions — the WHAT, not the results
# Results are filled in by Boss during testing
UAT_FEATURES = [
    {"id": 1,  "feature": "Provider Search (DE + MS + VA, vector + regex)"},
    {"id": 2,  "feature": "Specialty Identification (NUCC + AI expansion)"},
    {"id": 3,  "feature": "Clinical Trials Search (+ travel time)"},
    {"id": 4,  "feature": "About ChatHealthy / Skip Snow"},
    {"id": 5,  "feature": "Safety Filter (dual-trigger, IP lock, audit)"},
    {"id": 6,  "feature": "Lead Capture (follow-up offer)"},
    {"id": 7,  "feature": "Consent Framework (two-stream)"},
    {"id": 8,  "feature": "Provider Detail (NPI lookup + external links)"},
    {"id": 9,  "feature": "URL Guardian (validate + defang broken links)"},
    {"id": 10, "feature": "Chat UX (timer, stop, markdown, emergency)"},
    {"id": 11, "feature": "Blob Storage Infrastructure"},
    {"id": 12, "feature": "Unanswerable Question Handling (3-path)"},
    {"id": 13, "feature": "Markdown Table Rendering (GFM tables in chat)"},
    {"id": 14, "feature": "UAT Report (clean start, build number, environment banner)"},
    {"id": 15, "feature": "Session history preserved through safety unlock"},
    {"id": 16, "feature": "Long-running request timeout modal (30s threshold)"},
    {"id": 17, "feature": "CI/CD: Website auto-deploy on git push (roadmap propagation)"},
]


BUG_CLASSIFICATIONS = {
    "PE": "Prompt Engineering",
    "DB": "Database Fix",
    "LG": "Logic Fix",
    "MS": "Model Swap",
    "UX": "UX / Frontend Fix",
    "CF": "Configuration Fix",
    "IF": "Infrastructure Fix",
}

ENHANCEMENT_CLASSIFICATIONS = {
    "FT": "New Feature",
    "RF": "Refactor",
    "PF": "Performance",
    "DV": "DevOps",
    "SC": "Security",
}


def _get_uat_status(db, env_prefix: str, session_start: str = None) -> dict:
    """Read UAT status from MongoDB. Returns {feature_id: {done, bugs_fixed, new_features, notes}}.

    New session logic (session_start > record updated):
    - Bugs_fixed and new_features: KEPT (accumulate across sessions)
    - Notes: KEPT (bug history is valuable)
    - Done status: CLEARED for non-deferred features (re-test needed)
    - DEF status: KEPT (deferred stays deferred)
    """
    if db is None:
        return {}
    try:
        coll = db[f"{env_prefix}_System"]["uat_status"]
        doc = coll.find_one({"_id": "current"})
        if not doc or "features" not in doc:
            return {}
        status = {f["id"]: f for f in doc["features"]}
        # If session_start is newer than record, reset Done for non-deferred
        if session_start and doc.get("updated"):
            if session_start > doc["updated"]:
                _log.info("UAT new session %s > record %s — clearing non-deferred status", session_start, doc["updated"])
                for fid, feat in status.items():
                    if feat.get("done") not in ("DEF", "SIT"):
                        feat["done"] = ""
        return status
    except Exception:
        pass
    return {}


def update_uat_status(db, env_prefix: str, feature_id: int, done: str = None,
                      bugs_fixed: int = None, new_features: int = None, notes: str = None):
    """Update UAT status for a feature in MongoDB.

    Called by Claude when committing bug fixes or features.
    """
    if db is None:
        return
    coll = db[f"{env_prefix}_System"]["uat_status"]
    doc = coll.find_one({"_id": "current"})
    if not doc:
        doc = {"_id": "current", "features": []}
        coll.insert_one(doc)

    features = doc.get("features", [])
    existing = next((f for f in features if f["id"] == feature_id), None)
    if existing:
        if done is not None:
            existing["done"] = done
        if bugs_fixed is not None:
            existing["bugs_fixed"] = bugs_fixed
        if new_features is not None:
            existing["new_features"] = new_features
        if notes is not None:
            existing["notes"] = notes
    else:
        features.append({
            "id": feature_id,
            "done": done or "",
            "bugs_fixed": bugs_fixed or 0,
            "new_features": new_features or 0,
            "notes": notes or "",
        })

    from datetime import datetime, timezone
    coll.update_one({"_id": "current"}, {"$set": {"features": features, "updated": datetime.now(timezone.utc).isoformat()}})


def seed_uat_status(db, env_prefix: str):
    """Seed UAT status from current sprint work. Run once to populate."""
    updates = [
        {"id": 1,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 2,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 3,  "done": "",    "bugs_fixed": 1, "new_features": 0, "notes": "PE: international travel distance"},
        {"id": 4,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 5,  "done": "",    "bugs_fixed": 0, "new_features": 1, "notes": "FT: emergency keyword expansion (F-09)"},
        {"id": 6,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 7,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 8,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 9,  "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 10, "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 11, "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 12, "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 13, "done": "",    "bugs_fixed": 0, "new_features": 0, "notes": ""},
        {"id": 14, "done": "",    "bugs_fixed": 1, "new_features": 1, "notes": "UX: stale static bundles; FT: clean UAT report"},
        {"id": 15, "done": "",    "bugs_fixed": 0, "new_features": 1, "notes": "FT: session history through unlock"},
        {"id": 16, "done": "",    "bugs_fixed": 0, "new_features": 1, "notes": "FT: 30s timeout modal + abort"},
    ]
    coll = db[f"{env_prefix}_System"]["uat_status"]
    coll.replace_one({"_id": "current"}, {"_id": "current", "features": updates}, upsert=True)
    _log.info("UAT status seeded: %d features", len(updates))


def build_uat_welcome(get_db_fn=None, **overrides) -> str:
    """Build UAT welcome message. Reads all config from environment.

    Reads: ENV_PREFIX, APP_VERSION, HUMAN_TESTING, SPACE_ID from os.environ.
    Reads: build number + UAT status from MongoDB.
    Overrides: pass any param explicitly to override env detection.
    """
    import os
    env_prefix = overrides.get("env_prefix", os.getenv("ENV_PREFIX", "dev"))
    version = overrides.get("version", os.getenv("APP_VERSION", "unknown"))
    space_id = os.getenv("SPACE_ID")
    env = overrides.get("env", env_prefix if space_id else "local")
    human_testing_raw = overrides.get("session_start", os.getenv("HUMAN_TESTING", ""))
    session_start = human_testing_raw if len(human_testing_raw) > 5 else None

    # Build number from MongoDB
    db = get_db_fn() if get_db_fn else None
    build = "?"
    if db:
        try:
            record = db[f"{env_prefix}_System"]["build_counter"].find_one({"_id": "build"})
            build = str(record["number"]) if record else "0"
        except Exception:
            pass

    total = len(UAT_FEATURES)
    status = _get_uat_status(db, env_prefix, session_start=session_start)

    total_done = 0
    total_bugs = 0
    total_features = 0

    lines = [
        f"**UAT Session**\n\n"
        f"| Environment | Release | Build | Scenarios |\n"
        f"|:-----------:|:-------:|:-----:|:---------:|\n"
        f"| {env.upper()} | {version} | {build} | {total} |\n\n",
        "| # | Feature | Done | Bugs Fixed | New Features |",
        "|:---:|---------|:----:|:----------:|:------------:|",
    ]

    for f in UAT_FEATURES:
        s = status.get(f["id"], {})
        done = s.get("done", "")
        bugs = s.get("bugs_fixed", 0)
        features = s.get("new_features", 0)
        if done:
            total_done += 1
        total_bugs += bugs
        total_features += features
        lines.append(
            f"| {f['id']:>3} | {f['feature']} | {done} | {bugs or ''} | {features or ''} |"
        )

    lines.append(
        f"| | **TOTALS** | **{total_done}/{total}** | **{total_bugs}** | **{total_features}** |"
    )

    # Notes from DB
    has_notes = [(f, status.get(f["id"], {})) for f in UAT_FEATURES if status.get(f["id"], {}).get("notes")]
    if has_notes:
        lines.append("\n**Notes:**\n")
        for f, s in has_notes:
            lines.append(f"- **#{f['id']} {f['feature']}**: {s['notes']}")

    lines.append("\n**Bug Classifications:** " + " | ".join(f"**{k}**: {v}" for k, v in BUG_CLASSIFICATIONS.items()))
    lines.append("\n**Enhancement Classifications:** " + " | ".join(f"**{k}**: {v}" for k, v in ENHANCEMENT_CLASSIFICATIONS.items()))
    lines.append(
        "\n*Boss: mark Done (Y/DEF/FAIL), tally bugs and features as you test. Note type codes in chat.*"
    )

    return "\n".join(lines)
