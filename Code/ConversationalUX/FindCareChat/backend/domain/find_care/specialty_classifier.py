# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Specialty Classifier — uses GPT-4.1-mini to classify NUCC specialties
# as prescriber and/or homeopathic. Results cached in MongoDB.
#
# GOV-011 exception: AI makes classification decision (RISK-002 accepted).
# Cache is human-reviewable and correctable.

import json
import logging
import os
from datetime import datetime, timezone

_log = logging.getLogger("findcare.specialty_classifier")

_cache: dict[str, dict] = {}  # in-memory: code -> {can_prescribe, homeopathic}
_cache_loaded = False


def _get_db_cache(db):
    """Load cached classifications from MongoDB."""
    global _cache, _cache_loaded
    if _cache_loaded:
        return _cache
    try:
        env = os.environ.get("ENV_PREFIX", "dev")
        coll = db[f"{env}_PublicHealthData"]["specialty_classification"]
        for doc in coll.find({}, {"_id": 0}):
            code = doc.get("code", "")
            if code:
                _cache[code] = {
                    "can_prescribe": doc.get("can_prescribe", False),
                    "homeopathic": doc.get("homeopathic", False),
                    "source": doc.get("source", "unknown"),
                }
        _cache_loaded = True
        _log.info("Loaded %d specialty classifications from cache", len(_cache))
    except Exception as e:
        _log.warning("Failed to load specialty cache: %s", e)
        _cache_loaded = True
    return _cache


def _save_classifications(db, classifications: list[dict]):
    """Save classifications to MongoDB."""
    try:
        env = os.environ.get("ENV_PREFIX", "dev")
        coll = db[f"{env}_PublicHealthData"]["specialty_classification"]
        from pymongo import UpdateOne
        ops = []
        for c in classifications:
            ops.append(UpdateOne(
                {"code": c["code"]},
                {"$set": {
                    "code": c["code"],
                    "name": c["name"],
                    "can_prescribe": c["can_prescribe"],
                    "homeopathic": c["homeopathic"],
                    "source": "gpt-4.1-mini",
                    "classified_at": datetime.now(timezone.utc).isoformat(),
                }},
                upsert=True,
            ))
        if ops:
            coll.bulk_write(ops, ordered=False)
            _log.info("Saved %d specialty classifications to MongoDB", len(ops))
    except Exception as e:
        _log.warning("Failed to save specialty cache: %s", e)


def _classify_batch(specialties: list[dict]) -> list[dict]:
    """Call GPT-4.1-mini to classify specialties as prescriber and/or homeopathic.

    Input: [{code, name, classification}]
    Output: [{code, name, can_prescribe, homeopathic}]
    """
    from openai import OpenAI

    client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    specialty_list = "\n".join(
        f"{s['code']}: {s['name']}" for s in specialties
    )

    prompt = f"""Classify each healthcare specialty below on two dimensions:
1. can_prescribe: Can this provider type independently prescribe medications? (true/false)
2. homeopathic: Is this a homeopathic, naturopathic, or alternative medicine specialty? (true/false)

Return ONLY a JSON array. Each element: {{"code": "...", "can_prescribe": true/false, "homeopathic": true/false}}

Specialties:
{specialty_list}"""

    try:
        resp = client.chat.completions.create(
            model="gpt-4.1-mini",
            response_format={"type": "json_object"},
            max_tokens=4000,
            messages=[
                {"role": "system", "content": "You are a healthcare credentialing expert. Classify provider specialties accurately. Return valid JSON only."},
                {"role": "user", "content": prompt},
            ],
        )
        result = json.loads(resp.choices[0].message.content)
        # Handle both {classifications: [...]} and direct [...]
        items = result if isinstance(result, list) else result.get("classifications", result.get("specialties", []))
        _log.info("GPT classified %d specialties", len(items))
        return items
    except Exception as e:
        _log.error("GPT specialty classification failed: %s", e)
        return []
