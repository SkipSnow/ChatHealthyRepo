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


def classify_specialties(
    options: list[dict],
    db=None,
) -> list[dict]:
    """Enrich specialty options with can_prescribe and homeopathic flags.

    Checks cache first. Calls GPT-4.1-mini for uncached codes.
    Returns the same list with added fields.

    Input:  [{code, name, classification}]
    Output: [{code, name, classification, can_prescribe, homeopathic}]
    """
    if db:
        cache = _get_db_cache(db)
    else:
        cache = _cache

    uncached = []
    for opt in options:
        code = opt.get("code", "")
        if code not in cache:
            uncached.append(opt)

    # Classify uncached in one batch
    if uncached:
        _log.info("Classifying %d uncached specialties via GPT-4.1-mini", len(uncached))
        classified = _classify_batch(uncached)

        # Merge into cache
        for item in classified:
            code = item.get("code", "")
            if code:
                cache[code] = {
                    "can_prescribe": item.get("can_prescribe", False),
                    "homeopathic": item.get("homeopathic", False),
                    "source": "gpt-4.1-mini",
                }

        # Save to MongoDB
        if db and classified:
            enriched = []
            for item in classified:
                code = item.get("code", "")
                name = next((o["name"] for o in uncached if o.get("code") == code), "")
                enriched.append({
                    "code": code,
                    "name": name,
                    "can_prescribe": item.get("can_prescribe", False),
                    "homeopathic": item.get("homeopathic", False),
                })
            _save_classifications(db, enriched)

    # Enrich original options
    result = []
    for opt in options:
        code = opt.get("code", "")
        cls = cache.get(code, {})
        result.append({
            **opt,
            "can_prescribe": cls.get("can_prescribe", False),
            "homeopathic": cls.get("homeopathic", False),
        })

    return result
