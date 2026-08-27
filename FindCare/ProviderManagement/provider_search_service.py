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

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import os
from typing import Optional

from chathealthy_lib.runtime_data_collections import providers_coll, specialty_meta_coll

log = ChatHealthyLoggingService()


def primary_practice_address(p: dict) -> dict:
    """Return the first entry in practice_addresses[].

    v4 splits the single addresses[] into practice_addresses[] and
    business_address. Every element of practice_addresses IS a practice
    address, so the address_type test the old shape needed has nothing left
    to select on -- carried forward it would match nothing at all."""
    for a in p.get("practice_addresses") or []:
        if isinstance(a, dict):
            return a
    return {}


def matching_practice_address(p: dict, state: str = "", city: str = "",
                              county: str = "", zip: str = "") -> dict:
    """The practice address that satisfied the search, not merely the first.

    v03 held one practice address, so "the practice address" and "the first
    one" were the same thing. v4 holds several, and the query matches when
    ANY element satisfies the geography -- so a San Francisco search can
    return a provider whose element zero is in Wyoming, and displaying [0]
    shows an address that has nothing to do with what was asked for.

    Falls back to the first element when no criterion was given, which is
    the NPI and name routes where there is no geography to match.
    """
    wanted_state = (state or "").strip().upper()
    wanted_city = (city or "").strip().upper()
    wanted_county = (county or "").strip()
    wanted_zip = (zip or "").strip()[:5]
    if not (wanted_state or wanted_city or wanted_county or wanted_zip):
        return primary_practice_address(p)
    for a in p.get("practice_addresses") or []:
        if not isinstance(a, dict):
            continue
        if wanted_zip and (a.get("zip") or "")[:5] != wanted_zip:
            continue
        if wanted_state and (a.get("state") or "").upper() != wanted_state:
            continue
        if wanted_city and (a.get("city") or "").upper() != wanted_city:
            continue
        if wanted_county and (a.get("county") or {}).get("name") != wanted_county:
            continue
        return a
    return primary_practice_address(p)


def primary_county(p: dict) -> dict:
    """Return the county sub-doc on the primary practice address."""
    return primary_practice_address(p).get("county") or {}


DEFAULT_LIMIT = 25  # F-10: raised from 10

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
        self._taxonomy_name_cache = {}  # code -> Display Name

    def _searchable(self, base_filter: dict) -> dict:
        """The filter plus what every result is implicitly constrained to.

        Individuals only, and never a provider NPPES has deactivated.
        _facet_query applied these to its own copy, so anything computing
        counts off the raw filter counted providers the search would not
        return -- the panel promised 482 where the result held 477.
        """
        out = dict(base_filter)
        out["entity_type_code"] = "1"
        # active[] is written only when NPPES carries a deactivation or
        # reactivation date, so its absence is the whole test.
        out["active"] = {"$exists": False}
        return out

    # What NPPES appends to a county name. Louisiana has parishes, Alaska
    # boroughs, municipalities and census areas, Puerto Rico municipios,
    # Virginia and Maryland independent cities. Taken from the 2,104 distinct
    # names in the collection: no name carries any of these words other than
    # as its last, so stripping one can never eat part of a real name. 288
    # carry none at all, which is why the bare form is asked for too.
    COUNTY_TYPE_WORDS = ("County", "Parish", "Municipio", "city", "Borough",
                         "Area", "Region", "Municipality", "District")

    def _county_clause(self, county: str) -> dict:
        """Every stored form of one county name, as equality.

        The parameter is named county, so the kind-word is the field and not
        the value: the place is Los Angeles whether or not the person said
        'county'. The data appends the kind-word and which one depends on the
        state, so the value the model writes can never be the value the data
        holds. Code closes that here rather than asking the model to spell a
        stored value -- which is what made one sentence ask three different
        questions: 2,237 providers one run, the whole of California the next,
        nothing the run after.

        $in is a seek per value on the second key of
        idx_practice_state_county, so asking for every form costs what asking
        for one did.
        """
        bare = (county or "").strip()
        for word in self.COUNTY_TYPE_WORDS:
            suffix = " " + word
            if bare.endswith(suffix):
                bare = bare[:-len(suffix)]
                break
        return {"$in": [bare] + [f"{bare} {w}" for w in self.COUNTY_TYPE_WORDS]}

    def _name_filter(self, last: str = "", first: str = "", middle: str = "") -> dict:
        """Match a provider named outright.

        last is exact. first and middle match the name OR the bare initial,
        in either direction: a record may hold JAMES where the person typed
        J, or hold J where they typed JAMES, and a prefix only ever resolves
        the first of those. $in on the second and third keys of
        idx_provider_name is a seek per value rather than a scan.

        Everything is uppercased because NPPES stores names that way. A
        case-insensitive match would make the index unusable and turn every
        name search into a scan of 9.3M documents.
        """
        clause: dict = {}
        last = (last or "").strip().upper()
        if last:
            clause["provider_last_name_legal_name"] = last
        for field, value in (("provider_first_name", first),
                             ("provider_middle_name", middle)):
            value = (value or "").strip().upper()
            if not value:
                continue
            clause[field] = value if len(value) == 1 else {"$in": [value, value[0]]}
        return clause

    def _preference_filter(self, provider_sex: str = "",
                           sole_proprietor=None, insurance: str = "") -> dict:
        """Narrowings the person asked for, applied to an already-narrow set.

        None of these is indexed and none should be: each has a handful of
        values, so as a leading key it selects nothing. They refine a result
        specialty and geography have already cut to hundreds.

        A stated sex preference excludes everyone who does not match it,
        including X (neither male nor female, an affirmation) and U
        (undisclosed, a refusal to stipulate). Those two are never merged.
        """
        clause: dict = {}
        sex = (provider_sex or "").strip().upper()
        if sex:
            clause["provider_sex_code"] = sex
        if sole_proprietor is not None:
            clause["is_sole_proprietor"] = "Y" if sole_proprietor else "N"
        payer = (insurance or "").strip().upper()
        if payer:
            # Which payers a provider registered identifiers with. NOT
            # network participation -- nothing in NPPES establishes that.
            clause["insurance"] = {"$elemMatch": {"payer_name": payer}}
        return clause

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
        elem: dict = {}
        if z:
            elem["zip"] = z
        elif s and co:
            elem["state"] = s
            elem["county.name"] = self._county_clause(co)
        elif s and c:
            elem["state"] = s
            elem["city"] = c
        elif s:
            elem["state"] = s
        else:
            return {}
        return {"practice_addresses": {"$elemMatch": elem}}

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
        primary_code = p.get("primary_taxonomy_code")
        primary = next((t for t in p.get("taxonomies", [])
                        if t.get("code") == primary_code), None) if primary_code else None
        county_obj = primary_county(p)
        county_name = county_obj.get("name") or ""
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

        EPIC-006-F-002-S-002: results MUST NOT include institutions,
        facilities, agencies, or non-individual entries. Enforced two ways:
        (1) the Mongo query is forced to entity_type_code='1' (individuals);
        (2) after retrieval each returned doc is asserted as type-1 — if any
        is not, this is a data-integrity bug and we fail hard (no silent
        filtering, per Skip's 'no allowances for mistakes' rule).
        """
        base_filter = self._searchable(base_filter)
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
                raise ChatHealthyException(
                    mode="compliance_violation",
                    component="provider_search_service",
                    message="REQ-T-005 violation: non-individual provider returned by /search "
                    f"(npi={p.get('npi')!r}, entity_type_code={etc!r}). "
                    "Data-integrity bug — fail-hard per spec."
                )

    _PROJECTION = {
        "_id": 0, "npi": 1, "entity_type_code": 1,
        "provider_first_name": 1, "provider_last_name_legal_name": 1,
        "provider_middle_name": 1, "provider_name_prefix_text": 1,
        "provider_name_suffix_text": 1, "provider_credential_text": 1,
        "provider_organization_name_legal_business_name": 1,
        "practice_addresses": 1, "business_address": 1, "taxonomies": 1,
        "primary_taxonomy_code": 1,
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
                    "filter": {"practice_addresses.state": state},
                }
            },
            {"$match": precise or {"practice_addresses": {"$elemMatch": {"state": state}}}},
        ]
        pipeline += [{"$limit": limit}, {"$project": self._PROJECTION}]
        try:
            raw = list(providers_coll().aggregate(pipeline))
            return [self._format_provider(p) for p in raw]
        except Exception as e:
            # Mode 2 (REQ-B-008): provider vector search failed; user gets
            # empty results despite valid query. Search infrastructure
            # issue — operator MUST know.
            log.error("Vector search failed: %s", e, exc=ChatHealthyException(
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

    # What a person may still narrow by, and what each would cost them.
    #
    # Computed over the result specialty and geography have already cut to
    # hundreds, which is why none of these fields is indexed: as a leading
    # key each selects almost nothing, and here they are nearly free.
    #
    # The counts are the point. A sex preference costs 90% of the list for
    # orthopaedic surgery and 10% for nurse practitioners, and a person
    # should see that before choosing rather than after.
    SEX_LABELS = {"F": "Female", "M": "Male",
                  "X": "Neither Male nor Female", "U": "Undisclosed"}

    def _refinements(self, collection, base_filter: dict, already: dict) -> dict:
        """Per-choice counts for the dimensions not yet chosen."""
        # Every dimension is offered, including one already in force. Hiding
        # a chosen dimension left the person no way to undo it: the toggle
        # existed on the server and nothing on screen could reach it. A
        # filter you cannot remove is a trap.
        facets: dict = {}
        facets["sex"] = [{"$group": {"_id": "$provider_sex_code",
                                     "n": {"$sum": 1}}}]
        facets["sole_proprietor"] = [{"$group": {"_id": "$is_sole_proprietor",
                                                 "n": {"$sum": 1}}}]
        # Count PROVIDERS, not identifier rows. A provider registered with
        # one payer across several states carries several entries, so a
        # plain unwind reported 108 where 97 providers qualify -- an
        # overcount on the one screen whose job is to tell the truth about
        # what a choice costs.
        facets["insurance"] = [
            {"$unwind": "$insurance"},
            {"$group": {"_id": {"payer": "$insurance.payer_name",
                                "npi": "$npi"}}},
            {"$group": {"_id": "$_id.payer", "n": {"$sum": 1}}},
            {"$sort": {"n": -1}}, {"$limit": 8}]
        if not facets:
            return {}
        try:
            raw = next(iter(collection.aggregate(
                [{"$match": self._searchable(base_filter)},
                 {"$facet": facets}], allowDiskUse=True)))
        except Exception as exc:
            # Mode 1: refinement counts are an aid, not the answer. A
            # failure here leaves the panel without hints and the result
            # itself untouched.
            log.info("refinement counts unavailable: %s", exc,
                     exc=ChatHealthyException(
                         mode="refinement_counts_unavailable",
                         message=f"refinement counts unavailable: {exc}",
                         component="ProviderSearchService", exception=exc))
            return {}

        out: dict = {}
        if "sex" in raw:
            chosen = (already.get("provider_sex") or "").upper()
            rows = [{"value": r["_id"], "label": self.SEX_LABELS.get(r["_id"], r["_id"]),
                     "count": r["n"], "in_force": r["_id"] == chosen}
                    for r in raw["sex"] if r["_id"]]
            if len(rows) > 1 or chosen:
                out["provider_sex"] = sorted(rows, key=lambda r: -r["count"])
        if "sole_proprietor" in raw:
            # X is "Not Answered". Dropping it made the counts fail to sum
            # to the result total, which on a panel of costs reads as an
            # arithmetic error rather than as a provider who said nothing.
            labels = {"Y": "Yes", "N": "No", "X": "Not answered"}
            chosen_sole = already.get("sole_proprietor")
            in_force = None if chosen_sole is None else ("Y" if chosen_sole else "N")
            rows = [{"value": r["_id"], "label": labels.get(r["_id"], r["_id"]),
                     "count": r["n"], "in_force": r["_id"] == in_force}
                    for r in raw["sole_proprietor"] if r["_id"]]
            if len([r for r in rows if r["value"] in ("Y", "N")]) > 1 or in_force:
                out["sole_proprietor"] = sorted(rows, key=lambda r: -r["count"])
        if "insurance" in raw:
            chosen_ins = (already.get("insurance") or "").upper()
            rows = [{"value": r["_id"], "count": r["n"],
                     "in_force": r["_id"] == chosen_ins}
                    for r in raw["insurance"] if r["_id"]]
            if rows:
                out["insurance"] = rows
        return out

    def _paginated_result(self, providers: list, search_mode: str, safe_limit: int,
                          search_params: dict = None, total_count: int = 0,
                          page_start: int = 1, collection=None,
                          base_filter: dict = None, chosen: dict = None,
                          **extra) -> dict:
        """Build a result dict with pagination metadata and refinements.

        The refinements are computed HERE rather than at each route. Wired
        into one route only, four others returned providers with no way to
        narrow them -- including the county fallback -- and the panel went
        silent on exactly the searches that return the most.
        """
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
            "refinements": (
                self._refinements(collection, base_filter, chosen or {})
                if collection is not None and base_filter else {}),
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
                         last_name: str = "", first_name: str = "", middle_name: str = "",
                         provider_sex: str = "", sole_proprietor=None, insurance: str = "",
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
        chosen = {"provider_sex": provider_sex, "sole_proprietor": sole_proprietor,
                  "insurance": insurance}

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
        # A structured name uses idx_provider_name. The legacy free-text
        # `name` argument does not: it matched with an unanchored,
        # case-insensitive regex across three fields, which no index can
        # serve, so every such search scanned the whole collection.
        named = self._name_filter(last_name, first_name, middle_name)
        if named:
            base_filter = self._practice_address_filter(state=state_upper, city=city, county=county, zip=zip)
            base_filter.update(named)
            base_filter.update(self._preference_filter(provider_sex, sole_proprietor, insurance))
            providers, total_count = self._facet_query(collection, base_filter, after_npi, safe_limit)
            return self._paginated_result(providers, "name", safe_limit, search_params=_search_params,
                                          total_count=total_count,
                                          collection=collection, base_filter=base_filter,
                                          chosen=chosen)

        # ── Route 3: Specialty codes direct filter ──
        if specialty_codes:
            base_filter = {"taxonomies.code": {"$in": specialty_codes}}
            base_filter.update(self._practice_address_filter(state=state_upper, city=city, county=county, zip=zip))
            base_filter.update(self._preference_filter(provider_sex, sole_proprietor, insurance))

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
                # Mode 1 (REQ-B-008): specialty Display Names for the
                # summary are nice-to-have decoration; falls back to
                # specialization_options=[] and the summary message
                # degrades gracefully without names.
                log.info("specialty_meta options load failed (ignored): %s", _exc, exc=ChatHealthyException(
                                                                                       mode="specialty_meta_options_load_failed",
                                                                                       message=f"specialty_meta options load failed (ignored): {_exc}",
                                                                                       component="FindCareService",
                                                                                       exception=_exc,
                                                                                   ))

            # EPIC-006-F-002-S-002: the AI pipeline (incl. the
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
                                          collection=collection, base_filter=base_filter,
                                          chosen=chosen,
                                          codes_searched=len(specialty_codes))

        # ── Route 4: Specialty query (vector resolves codes → taxonomy returns data) ──
        if specialty_query:
            codes = []

            # EPIC-006-F-002-S-001: resolve codes via SpecialtyMetaData vector search only.
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
                # Mode 1 (REQ-B-008): homeopathic-specialty enrichment is
                # optional augmentation; without it, the base allopathic
                # specialization_options still surface. Result quality
                # degrades silently — no user-visible failure.
                log.info("Homeopathic resolver failed: %s", _he, exc=ChatHealthyException(
                                                                     mode="homeopathic_resolver_failed",
                                                                     message=f"Homeopathic resolver failed: {_he}",
                                                                     component="FindCareService",
                                                                     exception=_he,
                                                                 ))

            # Step 3: Database answers — deterministic taxonomy query
            base_filter = {"taxonomies.code": {"$in": codes}}
            base_filter.update(self._practice_address_filter(state=state_upper, city=city, county=county, zip=zip))
            base_filter.update(self._preference_filter(provider_sex, sole_proprietor, insurance))

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
                    # Mode 1 (REQ-B-008): admin debug log write; non-prod
                    # only, no user impact. Failure is silently ignored.
                    log.info("specialty_code_log write failed (ignored): %s", _exc, exc=ChatHealthyException(
                                                                                        mode="specialty_code_log_write_failed",
                                                                                        message=f"specialty_code_log write failed (ignored): {_exc}",
                                                                                        component="FindCareService",
                                                                                        exception=_exc,
                                                                                    ))

            # search_params includes resolved codes — /search replays with codes, no AI
            replay_params = {"state": state_upper, "nucc_codes": codes}
            if city: replay_params["city"] = city
            if county: replay_params["county"] = county
            if zip: replay_params["zip"] = zip
            return self._paginated_result(providers, "taxonomy", safe_limit,
                                          search_params=replay_params, total_count=total_count,
                                          collection=collection, base_filter=base_filter,
                                          chosen=chosen,
                                          state=state_upper, specialty_searched=specialty_query,
                                          specialization_options=specialization_options)

        # ── Route 5: County fallback ──
        if county and state_upper:
            base_filter = {
                "entity_type_code": "1",
                # v4 promotes the primary taxonomy to the document, so the
                # element match on the primary flag is a test of one scalar.
                # $regex is not permitted in this tree; a prefix range does
                # the same work on an indexable field.
                "primary_taxonomy_code": {"$gte": "2", "$lt": "3"},
            }
            base_filter.update(self._practice_address_filter(state=state_upper, county=county, zip=zip))
            base_filter.update(self._preference_filter(provider_sex, sole_proprietor, insurance))
            providers, total_count = self._facet_query(collection, base_filter, after_npi, safe_limit)
            log.info("search: county fallback returned %d for '%s' in %s", len(providers), county, state_upper)
            return self._paginated_result(providers, "county_physicians", safe_limit,
                                          search_params=_search_params, total_count=total_count,
                                          collection=collection, base_filter=base_filter,
                                          chosen=chosen,
                                          state=state_upper, county_searched=county)

        return {"supported": True, "providers": [],
                "message": f"No providers found matching the search criteria."}

    def identify_specialty(self, query: str) -> dict:
        """UAT Feature 2: Identify NUCC specialty codes via the two-stage
        AI pipeline (EPIC-006-F-002-S-002)."""
        if not self._specialty:
            return {"error": "SpecialtyFilter not configured"}
        return self._specialty.find_specialties(query)
