# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
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

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
import os
from typing import Optional

from chathealthy_frontend_lib.runtime_data_collections import providers_coll, specialty_meta_coll

log = ChatHealthyLoggingService()


def primary_practice_address(p: dict) -> dict:
    """Return the first entry in addresses[] whose address_type=='practice'."""
    for a in p.get("addresses") or []:
        if isinstance(a, dict) and a.get("address_type") == "practice":
            return a
    return {}


def primary_county(p: dict) -> dict:
    """Return the county sub-doc on the primary practice address."""
    return primary_practice_address(p).get("county") or {}


DEFAULT_LIMIT = 25  # F-10: raised from 10

# HACK ASN-4AFBDA: static FIPS-to-county seed
fips_to_county = {
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
        self.fips_to_county = dict(fips_to_county)
        self._taxonomy_name_cache = {}  # code -> Display Name
        self._load_fips_county_map()

    def _load_fips_county_map(self) -> None:
        db = self._get_db()
        if db is None:
            return
        try:
            agg = list(providers_coll().aggregate([
                {"$unwind": {"path": "$addresses", "preserveNullAndEmptyArrays": False}},
                {"$match": {
                    "addresses.address_type": "practice",
                    "addresses.county.fips": {"$exists": True},
                    "addresses.county.name": {"$exists": True},
                }},
                {"$group": {"_id": "$addresses.county.fips",
                            "name": {"$first": "$addresses.county.name"}}},
            ]))
            db_map = {p["_id"]: p["name"] for p in agg if p.get("_id")}
            self.fips_to_county.update(db_map)
            log.info("HACK ASN-4AFBDA: loaded %d FIPS mappings (%d from DB)", len(self.fips_to_county), len(db_map))
        except Exception as exc:
            log.warning("HACK ASN-4AFBDA: failed to load FIPS map: %s", exc, exc=ChatHealthyException(
                                                                              mode="fips_map_load_failed",
                                                                              message=f"HACK ASN-4AFBDA: failed to load FIPS map: {exc}",
                                                                              component="FindCareService",
                                                                              exception=exc,
                                                                          ), if_not_debug_log=True)

    def _practice_address_filter(self, state: str = "", city: str = "",
                                  county: str = "", zip: str = "") -> dict:
        """Build {"addresses": {"$elemMatch": ...}} for the practice address
        using deterministic priority (most-specific wins) with exact-match
        equality only — no regex:
          1. zip alone (most specific)
          2. county + state (per the operator's named ranking)
          3. city + state
          4. state alone
          5. nothing → {} (no location scoping)

        State is uppercased; city is uppercased to match the data shape
        (NPPES practice addresses store city in uppercase). County is
        passed through verbatim — the LLM's prompt instructs Title Case
        with the literal 'County' suffix, matching the data shape."""
        s = (state or "").strip().upper()
        c = (city or "").strip().upper()
        co = (county or "").strip()
        # Practice addresses store zip as 5 digits; truncate the incoming
        # value so a ZIP+4 emission from the LLM still matches.
        z = (zip or "").strip()[:5]
        elem = {"address_type": "practice"}
        if z:
            elem["zip"] = z
        elif s and co:
            elem["state"] = s
            elem["county.name"] = co
        elif s and c:
            elem["state"] = s
            elem["city"] = c
        elif s:
            elem["state"] = s
        else:
            return {}
        return {"addresses": {"$elemMatch": elem}}

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
        addr = primary_practice_address(p)
        address = ", ".join(x for x in [addr.get("line1"), addr.get("city"), addr.get("state"), addr.get("zip")] if x)
        primary = next((t for t in p.get("taxonomies", []) if t.get("primary")), None)
        county_obj = primary_county(p)
        county_name = county_obj.get("name") or self.fips_to_county.get(county_obj.get("fips", ""), "")
        raw_phone = addr.get("phone", "")
        phone = f"({raw_phone[:3]}) {raw_phone[3:6]}-{raw_phone[6:]}" if len(raw_phone) == 10 else raw_phone
        return {
            "name": name,
            "npi": p.get("npi", ""),
            "taxonomy_code": primary.get("code", "") if primary else "",
            "address": address,
            "phone": phone,
            "county": county_name,
            "lat": addr.get("lat"),
            "lng": addr.get("lng"),
        }

    def _facet_query(self, collection, base_filter: dict, after_npi: str, safe_limit: int) -> tuple:
        """Single-query count + page using $facet. Returns (providers, total_count).

        EPIC-006-F-002-S-002-REQ-T-005: results MUST NOT include institutions,
        facilities, agencies, or non-individual entries. Enforced two ways:
        (1) the Mongo query is forced to entity_type_code='1' (individuals);
        (2) after retrieval each returned doc is asserted as type-1 — if any
        is not, this is a data-integrity bug and we fail hard (no silent
        filtering, per Skip's 'no allowances for mistakes' rule).
        """
        base_filter = dict(base_filter)
        base_filter["entity_type_code"] = "1"
        query_filter = dict(base_filter)
        if after_npi:
            query_filter["npi"] = {"$gt": after_npi}
        pipeline = [
            {"$match": query_filter},
            {"$facet": {
                "count": [{"$match": base_filter}, {"$count": "total"}] if not after_npi
                          else [{"$count": "total"}],
                "page": [{"$sort": {"npi": 1}}] +
                         ([{"$limit": safe_limit}] if safe_limit > 0 else []) +
                         [{"$project": self._PROJECTION}],
            }},
        ]
        # For the count facet, we need the full base_filter count (not after_npi filtered)
        # So we run count separately only when paginating
        if after_npi:
            total_count = collection.count_documents(base_filter)
            result = list(collection.aggregate([
                {"$match": query_filter},
                {"$sort": {"npi": 1}},
            ] + ([{"$limit": safe_limit}] if safe_limit > 0 else []) + [
                {"$project": self._PROJECTION},
            ]))
            self._assert_individuals_only(result)
            return [self._format_provider(p) for p in result], total_count

        result = list(collection.aggregate(pipeline))
        if not result:
            return [], 0
        total_count = result[0]["count"][0]["total"] if result[0]["count"] else 0
        self._assert_individuals_only(result[0]["page"])
        providers = [self._format_provider(p) for p in result[0]["page"]]
        return providers, total_count

    def _assert_individuals_only(self, docs) -> None:
        """REQ-T-005 fail-hard: every returned provider MUST be
        entity_type_code='1'. A non-individual leaking through indicates a
        data-integrity bug; the app must abend so the operator sees it.
        """
        for p in docs:
            etc = p.get("entity_type_code")
            if etc != "1":
                raise RuntimeError(
                    "REQ-T-005 violation: non-individual provider returned by /search "
                    f"(npi={p.get('npi')!r}, entity_type_code={etc!r}). "
                    "Data-integrity bug — fail-hard per spec."
                )

    _PROJECTION = {
        "_id": 0, "npi": 1, "entity_type_code": 1,
        "provider_first_name": 1, "provider_last_name_legal_name": 1,
        "provider_middle_name": 1, "provider_name_prefix_text": 1,
        "provider_name_suffix_text": 1, "provider_credential_text": 1,
        "provider_organization_name_legal_business_name": 1,
        "addresses": 1, "taxonomies": 1,
    }

    def _vector_search(self, embedding: list, state: str, city: str, county: str, limit: int) -> list:
        db = self._get_db()
        if db is None:
            return []
        # $vectorSearch.filter does not support $elemMatch; the pre-filter is
        # lax on a dotted path and narrows candidates. The post-$match enforces
        # the precise practice-address scope (state + city + county on the
        # SAME array element) via $elemMatch through _practice_address_filter.
        precise = self._practice_address_filter(state=state, city=city, county=county)
        pipeline = [
            {
                "$vectorSearch": {
                    "index": "provider_vector_index",
                    "path": "embedding",
                    "queryVector": embedding,
                    "numCandidates": min(limit * 30, 300),
                    "limit": min(limit * 6, 60),
                    "filter": {"addresses.state": state, "addresses.address_type": "practice"},
                }
            },
            {"$match": precise or {"addresses": {"$elemMatch": {"address_type": "practice", "state": state}}}},
        ]
        pipeline += [{"$limit": limit}, {"$project": self._PROJECTION}]
        try:
            raw = list(providers_coll().aggregate(pipeline))
            return [self._format_provider(p) for p in raw]
        except Exception as e:
            log.warning("Vector search failed: %s", e, exc=ChatHealthyException(
                                                        mode="vector_search_failed",
                                                        message=f"Vector search failed: {e}",
                                                        component="FindCareService",
                                                        exception=e,
                                                    ), if_not_debug_log=True)
            return []

    @staticmethod
    def _build_summary_message(has_more: bool, total_count: int, page_count: int,
                               specialty_searched: str = "",
                               specialization_options: list = None,
                               **kwargs) -> str:
        """Build a system-generated summary message per GOV-011 / FC-RESULT-MSG.

        The system builds this message from structured data — the LLM does not write it.
        """
        if not has_more:
            return ""
        remaining = total_count - page_count
        search_term = specialty_searched or "results"
        spec_count = len(specialization_options) if specialization_options else 0
        # Build location narrowing options — omit the one they already used
        state = kwargs.get("state", "")
        city = kwargs.get("city", "")
        county = kwargs.get("county", "")
        narrow_options = []
        if not city:
            narrow_options.append("city")
        if not county:
            narrow_options.append("county")
        narrow_options.append("zipcode")

        parts = [f"There are {remaining:,} more '{search_term}'"]
        if state:
            parts.append(f" in '{state}'")
        parts.append(". ")
        if spec_count > 0:
            parts.append(f"There are {spec_count} types of providers. ")
        parts.append(f"Shall I show you [more '{search_term}'](#action:next-page)")
        if spec_count > 0:
            parts.append(f" or would you like to [filter '{search_term}' by provider type](#action:filter)?")
        else:
            parts.append("?")
        if narrow_options:
            parts.append(f" You can also search by {', '.join(narrow_options)}.")
        return "".join(parts)

    def _paginated_result(self, providers: list, search_mode: str, safe_limit: int,
                          search_params: dict = None, total_count: int = 0,
                          page_start: int = 1, **extra) -> dict:
        """Build a result dict with pagination metadata."""
        first_npi = providers[0]["npi"] if providers else ""
        last_npi = providers[-1]["npi"] if providers else ""
        has_more = len(providers) == safe_limit and safe_limit > 0
        page_end = page_start + len(providers) - 1 if providers else 0
        summary_message = self._build_summary_message(
            has_more, total_count, len(providers),
            specialty_searched=extra.get("specialty_searched", ""),
            specialization_options=extra.get("specialization_options"),
            state=extra.get("state", ""),
            city=(search_params or {}).get("city", ""),
            county=(search_params or {}).get("county", ""),
        )
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
            "summary_message": summary_message,
        }
        result.update(extra)
        return result

    def search_providers(self, specialty_query: str = "", state: str = "", city: str = "",
                         county: str = "", zip: str = "", limit: int = 25, npi: str = "", name: str = "",
                         nucc_codes: list[str] = None,
                         after_npi: str = "",
                         find_specialty_fn=None) -> dict:
        """Search for providers. Main entry point.

        Facade pattern (GoF) — routes to the right search strategy based on args.

        Args:
            specialty_query: natural-language specialty intent; resolved by the
                LLM tool to nucc_codes (no fuzzy matching anywhere).
            state: two-letter state code
            city: optional city filter
            county: optional county filter
            zip: optional ZIP code filter (5 digits)
            limit: max results
            npi: exact NPI lookup
            name: provider name search
            nucc_codes: list of exact NUCC taxonomy codes to filter by
            after_npi: keyset pagination — return results after this NPI (sorted by NPI ascending)
            find_specialty_fn: callable for taxonomy fallback (injected to avoid circular dep)
        """
        # Backwards-compat parameter name (the old `specialty_codes` is now `nucc_codes`).
        specialty_codes = nucc_codes
        state_upper = state.upper().strip() if state else ""

        # Build search_params for pagination replay
        _search_params = {}
        if specialty_query: _search_params["specialty_query"] = specialty_query
        if state_upper: _search_params["state"] = state_upper
        if city: _search_params["city"] = city
        if county: _search_params["county"] = county
        if zip: _search_params["zip"] = zip
        if name: _search_params["name"] = name
        if specialty_codes: _search_params["nucc_codes"] = specialty_codes

        db = self._get_db()
        if db is None:
            return {"error": "Database unavailable"}

        safe_limit = int(limit)
        collection = providers_coll()

        # ── Route 1: NPI exact lookup ──
        if npi:
            # REQ-T-005: individuals only. Filter at the query level so an
            # institution-NPI lookup returns "no provider found", and assert
            # post-fetch as a fail-hard guard against mislabeled records.
            provider = collection.find_one({"npi": npi, "entity_type_code": "1"}, self._PROJECTION)
            if provider:
                self._assert_individuals_only([provider])
                return {"supported": True, "search_mode": "npi", "count": 1,
                        "providers": [self._format_provider(provider)]}
            return {"supported": True, "providers": [], "message": f"No provider found for NPI {npi}."}

        # ── Route 2: Name search ──
        if name:
            base_filter = self._practice_address_filter(state=state_upper, city=city, county=county, zip=zip)
            base_filter["$or"] = [
                {"provider_last_name_legal_name": {"$regex": name.strip(), "$options": "i"}},
                {"provider_first_name": {"$regex": name.strip(), "$options": "i"}},
                {"provider_organization_name_legal_business_name": {"$regex": name.strip(), "$options": "i"}},
            ]
            providers, total_count = self._facet_query(collection, base_filter, after_npi, safe_limit)
            return self._paginated_result(providers, "name", safe_limit, search_params=_search_params,
                                          total_count=total_count)

        # ── Route 3: Specialty codes direct filter ──
        if specialty_codes:
            base_filter = {"taxonomies.code": {"$in": specialty_codes}}
            base_filter.update(self._practice_address_filter(state=state_upper, city=city, county=county, zip=zip))

            providers, total_count = self._facet_query(collection, base_filter, after_npi, safe_limit)
            log.info("search: specialty_codes returned %d for %d codes in %s",
                       len(providers), len(specialty_codes), state_upper or "all")

            # Look up selected specialty names for the summary
            specialization_options = []
            try:
                meta_coll = specialty_meta_coll()
                for doc in meta_coll.find({"Code": {"$in": specialty_codes}},
                                           {"Code": 1, "Display Name": 1, "can_prescribe": 1, "homeopathic": 1, "_id": 0}):
                    specialization_options.append({
                        "code": doc.get("Code", ""),
                        "name": doc.get("Display Name", ""),
                        "can_prescribe": doc.get("can_prescribe", False),
                        "homeopathic": doc.get("homeopathic", False),
                    })
            except Exception as _exc:
                log.warning("specialty_meta options load failed (ignored): %s", _exc, exc=ChatHealthyException(
                                                                                       mode="specialty_meta_options_load_failed",
                                                                                       message=f"specialty_meta options load failed (ignored): {_exc}",
                                                                                       component="FindCareService",
                                                                                       exception=_exc,
                                                                                   ), if_not_debug_log=True)

            # EPIC-006-F-002-S-002-REQ-T-012: the AI pipeline (incl. the
            # homeopathic resolver) runs once during /classify; results
            # are cached client-side per S-003-REQ-T-010. Apply Filter
            # MUST be a parameterized DB query only — no further LLM calls.

            # Build filtered search term from selected specialty names
            selected_names = [o["name"] for o in specialization_options if o.get("name")]
            filtered_term = ", ".join(selected_names) if selected_names else f"{len(specialty_codes)} selected specialties"

            return self._paginated_result(providers, "specialty_codes", safe_limit,
                                          search_params=_search_params, total_count=total_count,
                                          state=state_upper, specialty_searched=filtered_term,
                                          specialization_options=specialization_options,
                                          codes_searched=len(specialty_codes))

        # ── Route 4: Specialty query (vector resolves codes → taxonomy returns data) ──
        if specialty_query:
            codes = []

            # EPIC-006-F-002-S-001-REQ-T-001: resolve codes via SpecialtyMetaData vector search only.
            # No regex, no classify call. SpecialtyService embeds the query and matches
            # against NUCC specialty embeddings via cosine similarity.
            spec_fn = find_specialty_fn or (self.identify_specialty if self._specialty else None)
            if spec_fn:
                specialty_result = spec_fn(specialty_query)
                if "error" in specialty_result:
                    return specialty_result
                codes = [s["Code"] for s in specialty_result.get("specialties", [])]

            if not codes:
                return {"supported": True, "providers": [],
                        "message": f"No matching specialty found for '{specialty_query}'."}

            # Build specialization_options from vector search results (preserves rank)
            specialization_options = []
            for s in specialty_result.get("specialties", []):
                specialization_options.append({
                    "code": s.get("Code", ""),
                    "name": s.get("Display Name", ""),
                    "can_prescribe": s.get("can_prescribe", False),
                    "homeopathic": s.get("homeopathic", False),
                    "rank": s.get("rank", 0),
                })

            # Add relevant homeopathic specialties based on query context.
            # GPT-mini evaluates each homeopathic specialty against the search
            # query and returns strictly_compliant, loosely_compliant, or out_of_scope.
            try:
                from domain.find_care.homeopathic_resolver import resolve_homeopathic_specialties
                existing_codes = {o["code"] for o in specialization_options}
                homeo_options = resolve_homeopathic_specialties(
                    query=specialty_query or "",
                    existing_codes=existing_codes,
                    db=db,
                    env_prefix=self._env,
                )
                specialization_options.extend(homeo_options)
            except Exception as _he:
                log.warning("Homeopathic resolver failed: %s", _he, exc=ChatHealthyException(
                                                                     mode="homeopathic_resolver_failed",
                                                                     message=f"Homeopathic resolver failed: {_he}",
                                                                     component="FindCareService",
                                                                     exception=_he,
                                                                 ), if_not_debug_log=True)

            # Step 3: Database answers — deterministic taxonomy query
            base_filter = {"taxonomies.code": {"$in": codes}}
            base_filter.update(self._practice_address_filter(state=state_upper, city=city, county=county, zip=zip))

            providers, total_count = self._facet_query(collection, base_filter, after_npi, safe_limit)
            log.info("search: specialty '%s' → %d codes → %d/%d providers in %s",
                       specialty_query, len(codes), len(providers), total_count, state_upper or "all")

            # Log resolved codes to admin DB for debugging (non-prod only)
            if self._env != "prod":
                try:
                    db = self._get_db()
                    if db:
                        db[f"{self._env}_admin"]["specialty_code_log"].insert_one({
                            "query": specialty_query,
                            "resolved_codes": codes,
                            "code_count": len(codes),
                            "total_providers": total_count,
                            "state": state_upper,
                            "timestamp": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
                        })
                except Exception as _exc:
                    log.warning("specialty_code_log write failed (ignored): %s", _exc, exc=ChatHealthyException(
                                                                                        mode="specialty_code_log_write_failed",
                                                                                        message=f"specialty_code_log write failed (ignored): {_exc}",
                                                                                        component="FindCareService",
                                                                                        exception=_exc,
                                                                                    ), if_not_debug_log=True)

            # search_params includes resolved codes — /search replays with codes, no AI
            replay_params = {"state": state_upper, "nucc_codes": codes}
            if city: replay_params["city"] = city
            if county: replay_params["county"] = county
            if zip: replay_params["zip"] = zip
            return self._paginated_result(providers, "taxonomy", safe_limit,
                                          search_params=replay_params, total_count=total_count,
                                          state=state_upper, specialty_searched=specialty_query,
                                          specialization_options=specialization_options)

        # ── Route 5: County fallback ──
        if county and state_upper:
            base_filter = {
                "entity_type_code": "1",
                "taxonomies": {"$elemMatch": {"code": {"$regex": "^2"}, "primary": True}},
            }
            base_filter.update(self._practice_address_filter(state=state_upper, county=county, zip=zip))
            providers, total_count = self._facet_query(collection, base_filter, after_npi, safe_limit)
            log.info("search: county fallback returned %d for '%s' in %s", len(providers), county, state_upper)
            return self._paginated_result(providers, "county_physicians", safe_limit,
                                          search_params=_search_params, total_count=total_count,
                                          state=state_upper, county_searched=county)

        return {"supported": True, "providers": [],
                "message": f"No providers found matching the search criteria."}

    def identify_specialty(self, query: str) -> dict:
        """UAT Feature 2: Identify NUCC specialty codes via the two-stage
        AI pipeline (EPIC-006-F-002-S-002)."""
        if not self._specialty:
            return {"error": "SpecialtyFilter not configured"}
        return self._specialty.find_specialties(query)
