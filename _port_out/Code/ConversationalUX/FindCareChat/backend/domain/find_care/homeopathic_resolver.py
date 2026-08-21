# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
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

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.runtime_data_collections import specialty_meta_coll
from chathealthy_lib.exceptions import ChatHealthyException
import os

log = ChatHealthyLoggingService()


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
    if not query or db is None:
        return []

    # Load ALL specialties not already in the results — let GPT decide
    # which are relevant to homeopathic/alternative care for this query.
    # We don't pre-filter to our 24 homeopathic flags — GPT may identify
    # additional specialties that are relevant in a homeopathic context.
    # Databases are not named for environments; environments are separated
    # by cluster. The env-prefixed form pointed at a database that does not
    # exist, which is why specialty resolution returned nothing.
    meta_coll = specialty_meta_coll()
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
The user has CHECKED the "Homeopathic" filter. This means they WANT alternative/integrative medicine providers. They are actively looking for homeopathic, naturopathic, chiropractic, and other alternative care options.

For each specialty below, evaluate: COULD this provider type treat the user's condition or population?

IMPORTANT BIAS: The user wants alternative providers. Be INCLUSIVE, not exclusive.
- A naturopath CAN treat children, adults, elderly — they treat all ages
- An acupuncturist CAN treat musculoskeletal pain, anxiety, chronic conditions across all populations
- A chiropractor CAN treat patients of any age unless explicitly adult-only or geriatric-only
- A homeopath CAN treat any condition they practice for, regardless of patient demographics
- Only mark out_of_scope if the specialty is TRULY irrelevant (e.g., a midwife for tennis elbow)

Call the submit_specialty_evaluations tool with one entry per specialty.

Specialties:
{spec_list}"""

    # LLM contract: tool-use only. The model returns its result as a
    # tool_use block whose input is validated against the tool's
    # input_schema. No free-text post-processing, no regex.
    _TOOL = {
        "name": "submit_specialty_evaluations",
        "description": (
            "Submit relevance evaluations for each NUCC specialty for the "
            "user's homeopathic search. One element per specialty code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "specialties": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "code": {"type": "string"},
                            "relevance": {
                                "type": "string",
                                "enum": [
                                    "strictly_compliant",
                                    "loosely_compliant",
                                    "out_of_scope",
                                ],
                            },
                            "reason": {"type": "string"},
                        },
                        "required": ["code", "relevance", "reason"],
                    },
                },
            },
            "required": ["specialties"],
        },
    }

    try:
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=4000,
            system="You are a healthcare triage expert specializing in integrative and alternative medicine. The user has opted into homeopathic care — bias toward INCLUSION. Return your evaluation by calling the submit_specialty_evaluations tool.",
            messages=[
                {"role": "user", "content": prompt},
            ],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "submit_specialty_evaluations"},
        )
        result = {"specialties": []}
        for block in resp.content:
            if getattr(block, "type", None) == "tool_use":
                result = block.input or {"specialties": []}
                break
        items = result.get("specialties", [])
        log.info("Homeopathic resolver: %d specialties evaluated for '%s'", len(items), query[:40])
    except Exception as e:
        log.warning("Homeopathic resolver GPT call failed: %s", e, exc=ChatHealthyException(
                                                                    mode="homeopathic_resolver_gpt_failed",
                                                                    message=f"Homeopathic resolver GPT call failed: {e}",
                                                                    component="HomeopathicResolver",
                                                                    exception=e,
                                                                ), if_not_debug_log=True)
        return []

    # Build lookup from GPT results
    relevance_map = {item["code"]: item for item in items if item.get("code")}

    # Return only strictly and loosely compliant specialties
    options = []
    for doc in plausible:
        code = doc.get("Code", "")
        gpt_result = relevance_map.get(code, {})
        relevance = gpt_result.get("relevance") or gpt_result.get("classification", "out_of_scope")

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

    log.info("Homeopathic resolver: %d of %d specialties relevant for '%s'",
              len(options), len(plausible), query[:40])
    return options
