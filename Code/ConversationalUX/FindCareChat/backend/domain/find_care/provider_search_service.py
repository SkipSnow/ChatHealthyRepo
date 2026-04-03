# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# FindCareService — the public interface for all FindCare business capabilities.
#
# This is a Facade (GoF: https://refactoring.guru/design-patterns/facade)
# implemented as a service class. It is the single entry point for FindCare.
# EvaluateCareFacade calls this, never internal services directly.
#
# UAT Features: 1 (Provider Search), 2 (Specialty Identification)
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


class FindCareService:
    """FindCare Facade — single entry point for all FindCare capabilities.

    Facade pattern (GoF): simplifies access to provider search, specialty
    identification, and provider location. EvaluateCareFacade calls this
    service, never internal components directly.

    UAT Features: 1 (Provider Search), 2 (Specialty Identification)
    Dependencies: MongoDB (read-only), OpenAI embeddings, SpecialtyService.
    """

    def __init__(self, get_db_fn, env_prefix: str, get_embedding_fn, specialty_service=None):
        """
        Args:
            get_db_fn: callable returning MongoDB client or None
            env_prefix: environment prefix (dev/qa/prod)
            get_embedding_fn: callable(text) -> list[float] or None
        """
        self._get_db = get_db_fn
        self._env = env_prefix
        self._get_embedding = get_embedding_fn
        self._specialty = specialty_service
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

    def _paginated_result(self, providers: list, search_mode: str, safe_limit: int,
                          search_params: dict = None, total_count: int = 0,
                          page_start: int = 1, **extra) -> dict:
        """Build a result dict with pagination metadata."""
        first_npi = providers[0]["npi"] if providers else ""
        last_npi = providers[-1]["npi"] if providers else ""
        has_more = len(providers) == safe_limit and safe_limit > 0
        page_end = page_start + len(providers) - 1 if providers else 0
        result = {
            "supported": True,
            "search_mode": search_mode,
            "count": len(providers),
            "total_count": total_count,
            "providers": providers,
            "first_npi": first_npi,
            "last_npi": last_npi,
            "has_more": has_more,
            "page_start": page_start,
            "page_end": page_end,
            "search_params": search_params or {},
        }
        result.update(extra)
        return result

    def search_providers(self, specialty_query: str = "", state: str = "", city: str = "",
                         county: str = "", limit: int = 25, npi: str = "", name: str = "",
                         fuzzy_specialty: str = "", specialty_codes: list[str] = None,
                         after_npi: str = "",
                         find_specialty_fn=None) -> dict:
        """Search for providers. Main entry point.

        Facade pattern (GoF) — routes to the right search strategy based on args.

        Args:
            specialty_query: natural language specialty search (vector + taxonomy)
            state: two-letter state code
            city: optional city filter
            county: optional county filter
            limit: max results (default 25, ignored if fetch_all=True)
            npi: exact NPI lookup
            name: provider name search
            fuzzy_specialty: loose specialty text match -> identify_specialty -> codes -> filter
            specialty_codes: list of NUCC taxonomy codes to filter by directly
            fetch_all: if True, return all matching providers (overrides limit)
            after_npi: keyset pagination — return results after this NPI (sorted by NPI ascending)
            find_specialty_fn: callable for taxonomy fallback (injected to avoid circular dep)
        """
        # Route: fuzzy_specialty -> identify codes first, then search by codes
        if fuzzy_specialty and not specialty_codes:
            spec_result = self.identify_specialty(fuzzy_specialty)
            specialty_codes = [s["Code"] for s in spec_result.get("specialties", [])]
            if not specialty_codes:
                return {"supported": True, "providers": [],
                        "message": f"No matching specialty found for '{fuzzy_specialty}'."}

        state_upper = state.upper().strip() if state else ""
        if state_upper and state_upper not in SUPPORTED_STATES:
            return {
                "supported": False,
                "state": state_upper,
                "message": (
                    f"FindCare is currently available in Delaware (DE), Mississippi (MS), and Virginia (VA) only. "
                    f"We've noted interest in {state_upper}."
                ),
            }

        # Build search_params for pagination replay
        _search_params = {}
        if specialty_query: _search_params["specialty_query"] = specialty_query
        if state_upper: _search_params["state"] = state_upper
        if city: _search_params["city"] = city
        if county: _search_params["county"] = county
        if name: _search_params["name"] = name
        if specialty_codes: _search_params["specialty_codes"] = specialty_codes

        db = self._get_db()
        if db is None:
            return {"error": "Database unavailable"}

        safe_limit = int(limit)
        collection = db[f"{self._env}_PublicHealthData"]["providers"]

        # ── Route 1: NPI exact lookup ──
        if npi:
            provider = collection.find_one({"npi": npi}, self._PROJECTION)
            if provider:
                return {"supported": True, "search_mode": "npi", "count": 1,
                        "providers": [self._format_provider(provider)]}
            return {"supported": True, "providers": [], "message": f"No provider found for NPI {npi}."}

        # ── Route 2: Name search ──
        if name:
            base_filter = {"practice_address.state": state_upper} if state_upper else {}
            base_filter["$or"] = [
                {"provider_last_name_legal_name": {"$regex": name.strip(), "$options": "i"}},
                {"provider_first_name": {"$regex": name.strip(), "$options": "i"}},
                {"provider_organization_name_legal_business_name": {"$regex": name.strip(), "$options": "i"}},
            ]
            total_count = collection.count_documents(base_filter)
            query_filter = dict(base_filter)
            if after_npi:
                query_filter["npi"] = {"$gt": after_npi}
            cursor = collection.find(query_filter, self._PROJECTION).sort("npi", 1)
            if safe_limit > 0:
                cursor = cursor.limit(safe_limit)
            raw = list(cursor)
            providers = [self._format_provider(p) for p in raw]
            return self._paginated_result(providers, "name", safe_limit, search_params=_search_params,
                                          total_count=total_count)

        # ── Route 3: Specialty codes direct filter ──
        if specialty_codes:
            base_filter = {"taxonomies.code": {"$in": specialty_codes}}
            if state_upper:
                base_filter["practice_address.state"] = state_upper
            if city:
                base_filter["practice_address.city"] = {"$regex": city.strip(), "$options": "i"}
            if county:
                base_filter.update(self._make_county_filter(county))

            total_count = collection.count_documents(base_filter)
            query_filter = dict(base_filter)
            if after_npi:
                query_filter["npi"] = {"$gt": after_npi}
            cursor = collection.find(query_filter, self._PROJECTION).sort("npi", 1)
            if safe_limit > 0:
                cursor = cursor.limit(safe_limit)
            raw = list(cursor)
            providers = [self._format_provider(p) for p in raw]
            _log.info("search: specialty_codes returned %d for %d codes in %s",
                       len(providers), len(specialty_codes), state_upper or "all")
            return self._paginated_result(providers, "specialty_codes", safe_limit,
                                          search_params=_search_params, total_count=total_count,
                                          state=state_upper, codes_searched=len(specialty_codes))

        # ── Route 4: Vector search (natural language) ──
        if specialty_query:
            embedding = self._get_embedding(specialty_query)
            if embedding:
                vec_limit = safe_limit if safe_limit > 0 else DEFAULT_LIMIT
                providers = self._vector_search(embedding, state_upper, city, county, vec_limit)
                if providers:
                    # Get taxonomy codes from vector results to count total via taxonomy
                    result_codes = set()
                    for p in providers:
                        result_codes.add(p.get("taxonomy_code", ""))
                    result_codes.discard("")
                    total_count = len(providers)
                    if result_codes:
                        count_filter = {"taxonomies.code": {"$in": list(result_codes)}}
                        if state_upper:
                            count_filter["practice_address.state"] = state_upper
                        total_count = collection.count_documents(count_filter)
                    _log.info("search: vector returned %d (total %d) for '%s' in %s",
                              len(providers), total_count, specialty_query, state_upper)
                    return self._paginated_result(providers, "vector", safe_limit,
                                                  search_params=_search_params, total_count=total_count,
                                                  state=state_upper, specialty_searched=specialty_query)
                _log.info("search: vector returned 0, falling back to taxonomy")

            # Taxonomy fallback — use identify_specialty or injected fn
            spec_fn = find_specialty_fn or (self.identify_specialty if self._specialty else None)
            if spec_fn:
                specialty_result = spec_fn(specialty_query)
                if "error" in specialty_result:
                    return specialty_result
                codes = [s["Code"] for s in specialty_result.get("specialties", [])]
                if not codes:
                    return {"supported": True, "providers": [],
                            "message": f"No matching specialty found for '{specialty_query}'."}

                base_filter = {
                    "practice_address.state": state_upper,
                    "taxonomies.code": {"$in": codes},
                }
                if city:
                    base_filter["practice_address.city"] = {"$regex": city.strip(), "$options": "i"}
                if county:
                    base_filter.update(self._make_county_filter(county))

                total_count = collection.count_documents(base_filter)
                query_filter = dict(base_filter)
                if after_npi:
                    query_filter["npi"] = {"$gt": after_npi}
                cursor = collection.find(query_filter, self._PROJECTION).sort("npi", 1)
                if safe_limit > 0:
                    cursor = cursor.limit(safe_limit)
                raw = list(cursor)
                providers = [self._format_provider(p) for p in raw]
                _log.info("search: taxonomy returned %d for '%s' in %s", len(providers), specialty_query, state_upper)
                return self._paginated_result(providers, "taxonomy", safe_limit,
                                              search_params=_search_params, total_count=total_count,
                                              state=state_upper, specialty_searched=specialty_query)

        # ── Route 5: County fallback ──
        if county and state_upper:
            base_filter = {
                "practice_address.state": state_upper,
                "entity_type_code": "1",
                "taxonomies": {"$elemMatch": {"code": {"$regex": "^2"}, "primary": True}},
            }
            base_filter.update(self._make_county_filter(county))
            total_count = collection.count_documents(base_filter)
            county_filter = dict(base_filter)
            if after_npi:
                county_filter["npi"] = {"$gt": after_npi}
            cursor = collection.find(county_filter, self._PROJECTION).sort("npi", 1)
            if safe_limit > 0:
                cursor = cursor.limit(safe_limit)
            raw = list(cursor)
            providers = [self._format_provider(p) for p in raw]
            _log.info("search: county fallback returned %d for '%s' in %s", len(providers), county, state_upper)
            return self._paginated_result(providers, "county_physicians", safe_limit,
                                          search_params=_search_params, total_count=total_count,
                                          state=state_upper, county_searched=county)

        return {"supported": True, "providers": [],
                "message": f"No providers found matching the search criteria."}

    def identify_specialty(self, query: str) -> dict:
        """UAT Feature 2: Identify NUCC specialty codes."""
        if not self._specialty:
            return {"error": "SpecialtyService not configured"}
        return self._specialty.find_specialty_codes(query)

    def get_provider_location(self, npi: str) -> dict:
        """Return provider location for cross-domain travel calculations.

        Called by EvaluateCareFacade for clinical trial travel info.
        """
        db = self._get_db()
        if db is None:
            return {"npi": npi, "error": "Database unavailable"}
        try:
            provider = db[f"{self._env}_PublicHealthData"]["providers"].find_one(
                {"npi": npi}, {"practice_address": 1, "npi": 1, "_id": 0}
            )
            if provider:
                addr = provider.get("practice_address", {})
                return {
                    "npi": npi,
                    "lat": addr.get("lat"),
                    "lng": addr.get("lng"),
                    "address": ", ".join(x for x in [addr.get("line1"), addr.get("city"), addr.get("state"), addr.get("zip")] if x),
                }
            return {"npi": npi, "error": "Provider not found"}
        except Exception as e:
            _log.warning("get_provider_location failed for %s: %s", npi, e)
            return {"npi": npi, "error": str(e)}
