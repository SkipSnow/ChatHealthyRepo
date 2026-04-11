# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# Specialty Ranker — uses GPT-4.1-mini to sort specialty options
# by relevance to the user's search query. The most clinically
# relevant specialties appear first in the filter panel.
#
# One call per search. Fast (mini model). The ranking is contextual —
# "find me a kids doc" ranks Pediatrics first, "chest pain" ranks
# Cardiology first. Same list, different order.

import json
import logging
import os

from openai import OpenAI

_log = logging.getLogger("findcare.specialty_ranker")


# DEAD CODE (v4-031) -- unreferenced function 'rank_specialties', marked for deletion
# def rank_specialties(
#     options: list[dict],
#     query: str,
# ) -> list[dict]:
#     """Sort specialty options by relevance to the user's query.
#
#     Calls GPT-4.1-mini with the query and specialty names.
#     Returns the same list re-ordered by clinical relevance.
#     If ranking fails, returns the original list unchanged.
#
#     Input:  [{code, name, can_prescribe, homeopathic, ...}]
#     Output: same list, sorted by relevance to query
#     """
#     if not options or not query:
#         return options
#
#     if len(options) <= 3:
#         return options  # not worth ranking a tiny list
#
#     client = OpenAI(api_key=os.environ.get("OPENAI_API_KEY"))
#
#     # Build compact specialty list for the prompt
#     spec_list = "\n".join(f"{i+1}. {o['name']} ({o['code']})" for i, o in enumerate(options))
#
#     prompt = f"""A user searched for: "{query}"
#
# These specialties were found. Rank them from most relevant to least relevant for this search.
# Return ONLY a JSON object with key "order" containing an array of the specialty codes in ranked order.
#
# Specialties:
# {spec_list}"""
#
#     try:
#         resp = client.chat.completions.create(
#             model="gpt-4.1-mini",
#             response_format={"type": "json_object"},
#             max_tokens=2000,
#             messages=[
#                 {"role": "system", "content": "You are a healthcare triage expert. Rank specialties by clinical relevance to the patient's query. Return valid JSON only."},
#                 {"role": "user", "content": prompt},
#             ],
#         )
#         result = json.loads(resp.choices[0].message.content)
#         order = result.get("order", [])
#
#         if not order:
#             return options
#
#         # Build lookup and sort
#         code_to_opt = {o["code"]: o for o in options}
#         ranked = []
#         for code in order:
#             if code in code_to_opt:
#                 ranked.append(code_to_opt.pop(code))
#
#         # Append any that GPT missed (shouldn't happen, but defensive)
#         for opt in options:
#             if opt["code"] in code_to_opt:
#                 ranked.append(opt)
#
#         _log.info("Ranked %d specialties for query '%s'", len(ranked), query[:40])
#         return ranked
#
#     except Exception as e:
#         _log.warning("Specialty ranking failed: %s — returning unranked", e)
#         return options
# END DEAD CODE
