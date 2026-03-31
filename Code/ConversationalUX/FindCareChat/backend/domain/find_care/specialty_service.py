# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# SpecialtyService — UAT Feature 2: Specialty Identification (NUCC + AI)
#
# Extracted from main.py as part of ARCH-001 Phase 3.
# Host-independent — no FastAPI, no HuggingFace dependencies.
#
# Design: ARCH-001, business component: FindCare

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

_log = logging.getLogger("findcare.specialty")

INDIVIDUAL_PROVIDER_GROUPINGS = [
    "Allopathic & Osteopathic Physicians",
    "Behavioral Health & Social Service Providers",
    "Chiropractic Providers",
    "Dental Providers",
    "Dietary & Nutritional Service Providers",
    "Emergency Medical Service Providers",
    "Eye and Vision Services Providers",
    "Nursing Service Providers",
    "Nursing Service Related Providers",
    "Other Service Providers",
    "Pharmacy Service Providers",
    "Physician Assistants & Advanced Practice Nursing Providers",
    "Podiatric Medicine & Surgery Service Providers",
    "Respiratory, Developmental, Rehabilitative and Restorative Service Providers",
    "Speech, Language and Hearing Service Providers",
    "Student, Health Care",
    "Technologists, Technicians & Other Technical Service Providers",
]


class SpecialtyService:
    """Specialty identification: regex + vector dual pipeline.

    Dependencies: MongoDB (read-only), OpenAI embeddings, Anthropic Haiku (query expansion).
    """

    def __init__(self, get_db_fn, env_prefix: str, expand_query_fn, get_vector_fn):
        """
        Args:
            get_db_fn: callable returning MongoDB client or None
            env_prefix: environment prefix (dev/qa/prod)
            expand_query_fn: callable(query) -> list[str] for AI query expansion
            get_vector_fn: callable(query) -> list[float] for vector embedding
        """
        self._get_db = get_db_fn
        self._env = env_prefix
        self._expand_query = expand_query_fn
        self._get_vector = get_vector_fn

    def find_specialty_codes(self, query: str) -> dict:
        """Find NUCC specialty codes matching a query.

        Runs regex + vector pipelines in parallel, merges results.
        """
        db = self._get_db()
        if db is None:
            return {"error": "Database unavailable"}

        projection = {"_id": 0, "Code": 1, "Classification": 1, "Specialization": 1, "Display Name": 1}
        individual_filter = {"Grouping": {"$in": INDIVIDUAL_PROVIDER_GROUPINGS}}
        specialty_col = db[f"{self._env}_PublicHealthData"]["SpecialtyMetaData"]

        def regex_pipeline():
            stems = self._expand_query(query)
            regex_clauses = [
                {field: {"$regex": stem, "$options": "i"}}
                for stem in stems
                for field in ("Specialization", "Display Name")
            ]
            codes = list(specialty_col.find(
                {"$and": [{"$or": regex_clauses}, individual_filter]}, projection
            )) if regex_clauses else []
            return codes, stems

        def vector_pipeline():
            query_vector = self._get_vector(query)
            if not query_vector:
                return [], []
            top = list(specialty_col.aggregate([
                {"$vectorSearch": {
                    "index": "specialty_vector_index",
                    "path": "embedding",
                    "queryVector": query_vector,
                    "numCandidates": 100,
                    "limit": 5,
                }},
                {"$project": {"_id": 0, "Classification": 1, "score": {"$meta": "vectorSearchScore"}}},
            ]))
            classifications = list({m["Classification"] for m in top if m.get("score", 0) > 0.4})
            codes = list(specialty_col.find(
                {"$and": [{"Classification": {"$in": classifications}}, individual_filter]}, projection
            )) if classifications else []
            return codes, classifications

        with ThreadPoolExecutor(max_workers=2) as ex:
            rf = ex.submit(regex_pipeline)
            vf = ex.submit(vector_pipeline)
            try:
                regex_codes, stems = rf.result()
            except Exception as exc:
                _log.warning("regex_pipeline failed: %s", exc)
                regex_codes, stems = [], []
            try:
                vector_codes, classifications = vf.result()
            except Exception as exc:
                _log.warning("vector_pipeline failed: %s", exc)
                vector_codes, classifications = [], []

        seen, all_codes = set(), []
        for doc in vector_codes + regex_codes:
            if doc["Code"] not in seen:
                seen.add(doc["Code"])
                all_codes.append(doc)

        # Cap results — full NUCC dumps can exceed token budget
        all_codes = all_codes[:70]

        if "debug" in query.lower():
            return {
                "debug": True,
                "query": query,
                "stems_used_for_regex": stems,
                "classifications_from_vector_search": classifications,
                "total_codes_found": len(all_codes),
            }

        return {
            "specialties": [{"Code": c["Code"], "Display Name": c.get("Display Name", "")} for c in all_codes],
            "matched_classifications": classifications,
        }
