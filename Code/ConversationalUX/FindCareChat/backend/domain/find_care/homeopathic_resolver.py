# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Homeopathic Resolver — GPT-4.1-mini evaluates each homeopathic specialty
# against the user's search query and returns structured compliance flags.
#
# For each homeopathic specialty, GPT returns:
#   strictly_compliant  — directly relevant to the query
#   loosely_compliant   — plausibly relevant, wider interpretation
#   out_of_scope        — not relevant to this query
#
# Only strictly and loosely compliant specialties are added to the filter.
# RISK-002 accepted: AI makes contextual classification decision.

import json
import logging
import os

_log = logging.getLogger("findcare.homeopathic_resolver")


def resolve_homeopathic_specialties(
    query: str,
    existing_codes: set,
    db=None,
    env_prefix: str = "dev",
) -> list[dict]:
    """Evaluate homeopathic specialties against the user's query.

    Returns list of homeopathic specialty options with relevance flags,
    excluding any codes already in existing_codes.
    """
    if not query or not db:
        return []

    # Load ALL specialties not already in the results — let GPT decide
    # which are relevant to homeopathic/alternative care for this query.
    # We don't pre-filter to our 24 homeopathic flags — GPT may identify
    # additional specialties that are relevant in a homeopathic context.
    meta_coll = db[f"{env_prefix}_PublicHealthData"]["SpecialtyMetaData"]
    candidate_docs = list(meta_coll.find(
        {"Code": {"$nin": list(existing_codes)}},
        {"Code": 1, "Display Name": 1, "Classification": 1, "Grouping": 1,
         "can_prescribe": 1, "homeopathic": 1, "_id": 0},
    ))

    if not candidate_docs:
        return []

    # Send a compact list — just code + name (883 - existing ≈ 800+)
    # Too many for one prompt. Filter to plausible candidates first:
    # homeopathic-flagged + integrative/alternative/complementary in name
    plausible = []
    for d in candidate_docs:
        name = (d.get("Display Name") or d.get("Classification") or "").lower()
        grouping = (d.get("Grouping") or "").lower()
        is_flagged = d.get("homeopathic", False)
        is_alt = any(kw in name or kw in grouping for kw in [
            "naturo", "chiro", "acupunctur", "homeopath", "massage",
            "holistic", "integrative", "alternative", "complement",
            "wellness", "nutrition", "herbal", "ayurved", "midwife",
            "doula", "yoga", "meditation", "reiki", "reflexolog",
        ])
        if is_flagged or is_alt:
            plausible.append(d)

    if not plausible:
        return []

    spec_list = "\n".join(
        f"{d['Code']}: {d.get('Display Name') or d.get('Classification', '?')}"
        for d in plausible
    )

    from anthropic import Anthropic
    client = Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    prompt = f"""A user searched for: "{query}"

For each homeopathic/alternative medicine specialty below, evaluate whether it is relevant to this search.

Return ONLY a JSON object with key "specialties" containing an array.
Each element must have:
- "code": the NUCC code
- "relevance": one of "strictly_compliant", "loosely_compliant", or "out_of_scope"
- "reason": one sentence explanation

Rules:
- strictly_compliant: this specialty directly treats the condition or population the user is looking for
- loosely_compliant: this specialty could plausibly treat the user's concern with a broader interpretation
- out_of_scope: this specialty is not relevant to what the user is looking for

Specialties:
{spec_list}"""

    try:
        resp = client.messages.create(
            model="claude-sonnet-4-6",  # reasoning model for contextual medical relevance
            max_tokens=2000,
            system="You are a healthcare triage expert specializing in integrative and alternative medicine. Evaluate specialty relevance accurately. Return ONLY valid JSON, no other text.",
            messages=[
                {"role": "user", "content": prompt},
            ],
        )
        raw = resp.content[0].text
        # Extract JSON from response (Sonnet may wrap in markdown)
        if raw.strip().startswith("{"):
            result = json.loads(raw)
        else:
            import re
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            result = json.loads(match.group()) if match else {"specialties": []}
        items = result.get("specialties", [])
        _log.info("Homeopathic resolver: %d specialties evaluated for '%s'", len(items), query[:40])
    except Exception as e:
        _log.warning("Homeopathic resolver GPT call failed: %s", e)
        return []

    # Build lookup from GPT results
    relevance_map = {item["code"]: item for item in items if item.get("code")}

    # Return only strictly and loosely compliant specialties
    options = []
    for doc in plausible:
        code = doc.get("Code", "")
        gpt_result = relevance_map.get(code, {})
        relevance = gpt_result.get("relevance", "out_of_scope")

        if relevance in ("strictly_compliant", "loosely_compliant"):
            options.append({
                "code": code,
                "name": doc.get("Display Name") or doc.get("Classification", ""),
                "classification": doc.get("Classification", ""),
                "can_prescribe": doc.get("can_prescribe", False),
                "homeopathic": True,
                "homeopathic_relevance": relevance,
                "homeopathic_reason": gpt_result.get("reason", ""),
            })

    _log.info("Homeopathic resolver: %d of %d specialties relevant for '%s'",
              len(options), len(homeo_docs), query[:40])
    return options
