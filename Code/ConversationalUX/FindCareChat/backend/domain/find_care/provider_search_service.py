# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ProviderSearchService — UAT Feature 1: Provider Search (DE+MS+VA, vector+regex)
#
# Extracted from main.py as part of ARCH-001 Phase 2.
# Host-independent — no FastAPI, no HuggingFace dependencies.
#
# Design: ARCH-001, business component: FindCare

import logging
import os
from typing import Optional

_log = logging.getLogger("findcare.provider_search")

SUPPORTED_STATES = {"DE", "MS", "VA"}
DEFAULT_LIMIT = 25  # F-10: raised from 10

# HACK ASN-4AFBDA: static FIPS-to-county seed
_fips_to_county = {
    "10001": "Kent", "10003": "New Castle", "10005": "Sussex",
    "28001": "Adams", "28003": "Alcorn", "28005": "Amite", "28007": "Attala",
    "28009": "Benton", "28011": "Bolivar", "28013": "Calhoun",
}


class ProviderSearchService:
    """Provider search: vector + taxonomy fallback + county fallback.

    Dependencies: MongoDB (read-only), OpenAI embeddings.
    """

    def __init__(self, get_db_fn, env_prefix: str, get_embedding_fn):
        """
        Args:
            get_db_fn: callable returning MongoDB client or None
            env_prefix: environment prefix (dev/qa/prod)
            get_embedding_fn: callable(text) -> list[float] or None
        """
        self._get_db = get_db_fn
        self._env = env_prefix
        self._get_embedding = get_embedding_fn
        self._fips_to_county = dict(_fips_to_county)
        self._load_fips_county_map()

    def _load_fips_county_map(self) -> None:
        db = self._get_db()
        if db is None:
            return
        try:
            pairs = db[f"{self._env}_PublicHealthData"]["providers"].aggregate([
                {"$match": {"county.fips": {"$exists": True}, "county.name": {"$exists": True}}},
                {"$group": {"_id": "$county.fips", "name": {"$first": "$county.name"}}},
            ])
            db_map = {p["_id"]: p["name"] for p in pairs}
            self._fips_to_county.update(db_map)
            _log.info("HACK ASN-4AFBDA: loaded %d FIPS mappings (%d from DB)", len(self._fips_to_county), len(db_map))
        except Exception as exc:
            _log.warning("HACK ASN-4AFBDA: failed to load FIPS map: %s", exc)

    def _make_county_filter(self, county: str) -> dict:
        term = county.strip().lower()
        name_filter = {"county.name": {"$regex": county.strip(), "$options": "i"}}
        matching_fips = [fips for fips, name in self._fips_to_county.items() if term in name.lower()]
        if matching_fips:
            return {"$or": [name_filter, {"county.fips": {"$in": matching_fips}}]}
        return name_filter

    def _format_provider(self, p: dict) -> dict:
        if p.get("entity_type_code") == "1":
            parts = [
                p.get("provider_name_prefix_text"), p.get("provider_first_name"),
                p.get("provider_middle_name"), p.get("provider_last_name_legal_name"),
                p.get("provider_name_suffix_text"),
            ]
            name = " ".join(x for x in parts if x)
            if p.get("provider_credential_text"):
                name += f", {p['provider_credential_text']}"
        else:
            name = p.get("provider_organization_name_legal_business_name") or "Unknown Organization"
        addr = p.get("practice_address", {})
        address = ", ".join(x for x in [addr.get("line1"), addr.get("city"), addr.get("state"), addr.get("zip")] if x)
        primary = next((t for t in p.get("taxonomies", []) if t.get("primary")), None)
        county_obj = p.get("county", {})
        county_name = county_obj.get("name") or self._fips_to_county.get(county_obj.get("fips", ""), "")
        raw_phone = addr.get("phone", "")
        phone = f"({raw_phone[:3]}) {raw_phone[3:6]}-{raw_phone[6:]}" if len(raw_phone) == 10 else raw_phone
        return {
            "name": name,
            "npi": p.get("npi", ""),
            "taxonomy_code": primary.get("code", "") if primary else "",
            "address": address,
            "phone": phone,
            "county": county_name,
            "lat": p.get("practice_address", {}).get("lat"),
            "lng": p.get("practice_address", {}).get("lng"),
        }

    _PROJECTION = {
        "_id": 0, "npi": 1, "entity_type_code": 1,
        "provider_first_name": 1, "provider_last_name_legal_name": 1,
        "provider_middle_name": 1, "provider_name_prefix_text": 1,
        "provider_name_suffix_text": 1, "provider_credential_text": 1,
        "provider_organization_name_legal_business_name": 1,
        "practice_address": 1, "taxonomies": 1, "county": 1,
    }

    def _vector_search(self, embedding: list, state: str, city: str, county: str, limit: int) -> list:
        db = self._get_db()
        if db is None:
            return []
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "provider_vector_index",
                    "path": "embedding",
                    "queryVector": embedding,
                    "numCandidates": min(limit * 30, 300),
                    "limit": min(limit * 6, 60),
                    "filter": {"practice_address.state": state},
                }
            },
            {"$match": {"practice_address.state": state}},
        ]
        if city:
            pipeline.append({"$match": {"practice_address.city": {"$regex": city.strip(), "$options": "i"}}})
        if county:
            pipeline.append({"$match": self._make_county_filter(county)})
        pipeline += [{"$limit": limit}, {"$project": self._PROJECTION}]
        try:
            raw = list(db[f"{self._env}_PublicHealthData"]["providers"].aggregate(pipeline))
            return [self._format_provider(p) for p in raw]
        except Exception as e:
            _log.warning("Vector search failed: %s", e)
            return []

    def search(self, specialty_query: str, state: str, city: str = "", county: str = "",
               limit: int = 5, find_specialty_fn=None) -> dict:
        """Search for providers. Main entry point.

        Args:
            specialty_query: what kind of provider to find
            state: two-letter state code
            city: optional city filter
            county: optional county filter
            limit: max results (capped at DEFAULT_LIMIT)
            find_specialty_fn: callable for taxonomy fallback (injected to avoid circular dep)
        """
        state_upper = state.upper().strip()
        if state_upper not in SUPPORTED_STATES:
            return {
                "supported": False,
                "state": state_upper,
                "message": (
                    f"FindCare is currently available in Delaware (DE), Mississippi (MS), and Virginia (VA) only. "
                    f"We've noted interest in {state_upper}."
                ),
            }

        db = self._get_db()
        if db is None:
            return {"error": "Database unavailable"}

        safe_limit = min(int(limit), DEFAULT_LIMIT)

        # Vector search
        embedding = self._get_embedding(specialty_query)
        if embedding:
            providers = self._vector_search(embedding, state_upper, city, county, safe_limit)
            if providers:
                _log.info("search: vector returned %d for '%s' in %s", len(providers), specialty_query, state_upper)
                return {"supported": True, "state": state_upper, "specialty_searched": specialty_query,
                        "search_mode": "vector", "count": len(providers), "providers": providers}
            _log.info("search: vector returned 0, falling back to taxonomy")

        # Taxonomy fallback
        if find_specialty_fn:
            specialty_result = find_specialty_fn(specialty_query)
            if "error" in specialty_result:
                return specialty_result
            codes = [s["Code"] for s in specialty_result.get("specialties", [])]
            if not codes:
                return {"supported": True, "providers": [], "message": f"No matching specialty found for '{specialty_query}'."}

            query_filter = {
                "practice_address.state": state_upper,
                "taxonomies.code": {"$in": codes},
            }
            if city:
                query_filter["practice_address.city"] = {"$regex": city.strip(), "$options": "i"}
            if county:
                query_filter.update(self._make_county_filter(county))

            raw = list(
                db[f"{self._env}_PublicHealthData"]["providers"]
                .find(query_filter, self._PROJECTION)
                .limit(safe_limit)
            )
            if raw:
                providers = [self._format_provider(p) for p in raw]
                _log.info("search: taxonomy returned %d for '%s' in %s", len(providers), specialty_query, state_upper)
                return {"supported": True, "state": state_upper, "specialty_searched": specialty_query,
                        "search_mode": "taxonomy", "count": len(providers), "providers": providers}

        # County fallback
        if county:
            county_filter = {
                "practice_address.state": state_upper,
                "entity_type_code": "1",
                "taxonomies": {"$elemMatch": {"code": {"$regex": "^2"}, "primary": True}},
            }
            county_filter.update(self._make_county_filter(county))
            raw = list(
                db[f"{self._env}_PublicHealthData"]["providers"]
                .find(county_filter, self._PROJECTION)
                .limit(safe_limit)
            )
            if raw:
                providers = [self._format_provider(p) for p in raw]
                _log.info("search: county fallback returned %d for '%s' in %s", len(providers), county, state_upper)
                return {"supported": True, "state": state_upper, "county_searched": county,
                        "search_mode": "county_physicians", "count": len(providers), "providers": providers}

        return {"supported": True, "providers": [], "message": f"No {specialty_query} providers found in {state_upper}."}
