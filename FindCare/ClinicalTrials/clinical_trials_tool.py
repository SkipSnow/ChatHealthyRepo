# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""ClinicalTrialsTool — EPIC-006-F-031.

Fetches recruiting trials from ClinicalTrials.gov v2 by condition, enriches
each location with drive distance + duration from the user's location via
Google Routes API (skipped when user_location is absent), derives a per-
trial NUCC code set via one batched LLM call, and attaches an NPI to any
centralContact whose name matches a single provider in the ChatHealthy
providers collection whose taxonomies intersect the derived code set.

A single outer try/except converts any exception to an empty result with
an error message — REQ-B-012 says no fatal exception escapes."""
from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import httpx

from chathealthy_frontend_lib import ChatHealthyLoggingService
from chathealthy_frontend_lib.exceptions import ChatHealthyException
from chathealthy_frontend_lib.runtime_data_collections import providers_coll

from authentication.agent_deps import AgentDeps
from authentication.chathealthy_tool import ChatHealthyTool

try:
    from ClinicalTrials.clinical_trials_models import (
        ArmGroup, BioSpec, BrowseBranch, BrowseLeaf, CentralContact,
        Collaborator, DesignInfo, Intervention, IpdSharing, LargeDoc,
        Location, Organization, Outcome, OverallOfficial, Reference,
        Request, Response, ResponsibleParty, SecondaryId, SeeAlsoLink,
        Trial, UnpostedAnnotation, UnpostedEvent, ViolationAnnotation,
        ViolationEvent,
    )
    from ClinicalTrials.nucc_llm import derive_nucc_codes_for_page
except ImportError:
    from FindCare.ClinicalTrials.clinical_trials_models import (
        ArmGroup, BioSpec, BrowseBranch, BrowseLeaf, CentralContact,
        Collaborator, DesignInfo, Intervention, IpdSharing, LargeDoc,
        Location, Organization, Outcome, OverallOfficial, Reference,
        Request, Response, ResponsibleParty, SecondaryId, SeeAlsoLink,
        Trial, UnpostedAnnotation, UnpostedEvent, ViolationAnnotation,
        ViolationEvent,
    )
    from FindCare.ClinicalTrials.nucc_llm import derive_nucc_codes_for_page

log = ChatHealthyLoggingService()

CT_GOV_URL = "https://clinicaltrials.gov/api/v2/studies"
ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_MAPS_API_KEY_ENV = "GOOGLE_MAPS_API_KEY"


def _s(v) -> str:
    return (v or "").strip() if isinstance(v, str) else ""


def _parse_trial(study: dict) -> Trial:
    ps = study.get("protocolSection", {}) or {}
    id_mod = ps.get("identificationModule", {}) or {}
    status_mod = ps.get("statusModule", {}) or {}
    spons_mod = ps.get("sponsorCollaboratorsModule", {}) or {}
    over_mod = ps.get("oversightModule", {}) or {}
    desc_mod = ps.get("descriptionModule", {}) or {}
    cond_mod = ps.get("conditionsModule", {}) or {}
    design_mod = ps.get("designModule", {}) or {}
    arms_mod = ps.get("armsInterventionsModule", {}) or {}
    outcomes_mod = ps.get("outcomesModule", {}) or {}
    elig_mod = ps.get("eligibilityModule", {}) or {}
    contacts_mod = ps.get("contactsLocationsModule", {}) or {}
    refs_mod = ps.get("referencesModule", {}) or {}
    ipd_mod = ps.get("ipdSharingStatementModule", {}) or {}
    derived = study.get("derivedSection", {}) or {}
    doc_section = study.get("documentSection", {}) or {}
    annot = study.get("annotationSection", {}) or {}

    nct_id = id_mod.get("nctId", "")

    # identification
    org_raw = id_mod.get("organization", {}) or {}
    organization = Organization(
        full_name=_s(org_raw.get("fullName")),
        **{"class": _s(org_raw.get("class"))},
    )
    secondary_id_infos = [
        SecondaryId(
            id=_s(s.get("id")),
            type=_s(s.get("type")),
            domain=_s(s.get("domain")),
        )
        for s in (id_mod.get("secondaryIdInfos") or [])
    ]

    # sponsorCollaborators
    lead_sp = spons_mod.get("leadSponsor", {}) or {}
    rp_raw = spons_mod.get("responsibleParty", {}) or {}
    responsible_party = ResponsibleParty(
        type=_s(rp_raw.get("type")),
        investigator_full_name=_s(rp_raw.get("investigatorFullName")),
        investigator_title=_s(rp_raw.get("investigatorTitle")),
        investigator_affiliation=_s(rp_raw.get("investigatorAffiliation")),
        old_name_title=_s(rp_raw.get("oldNameTitle")),
        old_organization=_s(rp_raw.get("oldOrganization")),
    )
    collaborators = [
        Collaborator(
            name=_s(c.get("name")),
            **{"class": _s(c.get("class"))},
        )
        for c in (spons_mod.get("collaborators") or [])
    ]

    # design
    di_raw = design_mod.get("designInfo", {}) or {}
    masking_raw = di_raw.get("maskingInfo", {}) or {}
    design_info = DesignInfo(
        allocation=_s(di_raw.get("allocation")),
        intervention_model=_s(di_raw.get("interventionModel")),
        intervention_model_description=_s(di_raw.get("interventionModelDescription")),
        primary_purpose=_s(di_raw.get("primaryPurpose")),
        observational_model=_s(di_raw.get("observationalModel")),
        time_perspective=_s(di_raw.get("timePerspective")),
        masking=_s(masking_raw.get("masking")),
        masking_description=_s(masking_raw.get("maskingDescription")),
        who_masked=[_s(w) for w in (masking_raw.get("whoMasked") or []) if w],
    )
    bs_raw = design_mod.get("bioSpec", {}) or {}
    bio_spec = BioSpec(
        retention=_s(bs_raw.get("retention")),
        description=_s(bs_raw.get("description")),
    )
    enrollment_info = design_mod.get("enrollmentInfo", {}) or {}
    enrollment = enrollment_info.get("count")

    # armsInterventions
    arm_groups = [
        ArmGroup(
            label=_s(a.get("label")),
            type=_s(a.get("type")),
            description=_s(a.get("description")),
            intervention_names=[_s(n) for n in (a.get("interventionNames") or []) if n],
        )
        for a in (arms_mod.get("armGroups") or [])
    ]
    interventions = [
        Intervention(
            type=_s(i.get("type")),
            name=_s(i.get("name")),
            description=_s(i.get("description")),
            arm_group_labels=[_s(l) for l in (i.get("armGroupLabels") or []) if l],
            other_names=[_s(n) for n in (i.get("otherNames") or []) if n],
        )
        for i in (arms_mod.get("interventions") or [])
    ]

    # outcomes
    def _outcomes(key):
        return [
            Outcome(
                measure=_s(o.get("measure")),
                description=_s(o.get("description")),
                time_frame=_s(o.get("timeFrame")),
            )
            for o in (outcomes_mod.get(key) or [])
        ]
    primary_outcomes = _outcomes("primaryOutcomes")
    secondary_outcomes = _outcomes("secondaryOutcomes")
    other_outcomes = _outcomes("otherOutcomes")

    # contactsLocations
    locations: list[Location] = []
    for loc in contacts_mod.get("locations") or []:
        locations.append(Location(
            facility=_s(loc.get("facility")),
            status=_s(loc.get("status")),
            city=_s(loc.get("city")),
            state=_s(loc.get("state")),
            country=_s(loc.get("country")),
            zip=_s(loc.get("zip")),
        ))

    central_contacts: list[CentralContact] = []
    for c in contacts_mod.get("centralContacts") or []:
        central_contacts.append(CentralContact(
            name=_s(c.get("name")),
            role=_s(c.get("role")),
            phone=_s(c.get("phone")),
            phone_ext=_s(c.get("phoneExt")) or None,
            email=_s(c.get("email")) or None,
        ))

    overall_officials: list[OverallOfficial] = []
    for o in contacts_mod.get("overallOfficials") or []:
        overall_officials.append(OverallOfficial(
            name=_s(o.get("name")),
            affiliation=_s(o.get("affiliation")),
            role=_s(o.get("role")),
        ))

    # references
    references = [
        Reference(
            pmid=_s(r.get("pmid")),
            type=_s(r.get("type")),
            citation=_s(r.get("citation")),
        )
        for r in (refs_mod.get("references") or [])
    ]
    see_also_links = [
        SeeAlsoLink(label=_s(l.get("label")), url=_s(l.get("url")))
        for l in (refs_mod.get("seeAlsoLinks") or [])
    ]

    # ipdSharingStatement
    ipd_sharing = IpdSharing(
        ipd_sharing=_s(ipd_mod.get("ipdSharing")),
        description=_s(ipd_mod.get("description")),
        info_types=[_s(t) for t in (ipd_mod.get("infoTypes") or []) if t],
        time_frame=_s(ipd_mod.get("timeFrame")),
        access_criteria=_s(ipd_mod.get("accessCriteria")),
        url=_s(ipd_mod.get("url")),
    )

    # derivedSection
    cond_browse = derived.get("conditionBrowseModule", {}) or {}
    interv_browse = derived.get("interventionBrowseModule", {}) or {}
    def _leaves(src):
        return [
            BrowseLeaf(
                id=_s(b.get("id")),
                name=_s(b.get("name")),
                as_found=_s(b.get("asFound")),
                relevance=_s(b.get("relevance")),
            )
            for b in (src.get("browseLeaves") or [])
        ]
    def _branches(src):
        return [
            BrowseBranch(abbrev=_s(b.get("abbrev")), name=_s(b.get("name")))
            for b in (src.get("browseBranches") or [])
        ]
    condition_browse_leaves = _leaves(cond_browse)
    condition_browse_branches = _branches(cond_browse)
    intervention_browse_leaves = _leaves(interv_browse)
    intervention_browse_branches = _branches(interv_browse)

    # documentSection
    large_doc_mod = doc_section.get("largeDocumentModule", {}) or {}
    large_docs = []
    for d in (large_doc_mod.get("largeDocs") or []):
        size_v = d.get("size")
        large_docs.append(LargeDoc(
            type_abbrev=_s(d.get("typeAbbrev")),
            has_protocol=bool(d.get("hasProtocol", False)),
            has_sap=bool(d.get("hasSap", False)),
            has_icf=bool(d.get("hasIcf", False)),
            label=_s(d.get("label")),
            date=_s(d.get("date")),
            upload_date=_s(d.get("uploadDate")),
            filename=_s(d.get("filename")),
            size=int(size_v) if isinstance(size_v, int) else None,
        ))

    # annotationSection
    unposted_raw = annot.get("unpostedAnnotation", {}) or {}
    unposted_annotation = UnpostedAnnotation(
        unposted_responsible_party=_s(unposted_raw.get("unpostedResponsibleParty")),
        unposted_events=[
            UnpostedEvent(type=_s(e.get("type")), date=_s(e.get("date")))
            for e in (unposted_raw.get("unpostedEvents") or [])
        ],
    )
    violation_raw = annot.get("violationAnnotation", {}) or {}
    violation_annotation = ViolationAnnotation(
        violation_events=[
            ViolationEvent(
                type=_s(e.get("type")),
                description=_s(e.get("description")),
                creation_date=_s(e.get("creationDate")),
                issued_date=_s(e.get("issuedDate")),
                release_date=_s(e.get("releaseDate")),
                posted_date=_s(e.get("postedDate")),
            )
            for e in (violation_raw.get("violationEvents") or [])
        ],
    )

    return Trial(
        nct_id=nct_id,
        brief_title=_s(id_mod.get("briefTitle")),
        official_title=_s(id_mod.get("officialTitle")),
        acronym=_s(id_mod.get("acronym")),
        organization=organization,
        secondary_id_infos=secondary_id_infos,
        overall_status=_s(status_mod.get("overallStatus")),
        last_known_status=_s(status_mod.get("lastKnownStatus")),
        start_date=_s((status_mod.get("startDateStruct") or {}).get("date")),
        primary_completion_date=_s((status_mod.get("primaryCompletionDateStruct") or {}).get("date")),
        last_update_post_date=_s((status_mod.get("lastUpdatePostDateStruct") or {}).get("date")),
        study_first_submit_date=_s(status_mod.get("studyFirstSubmitDate")),
        lead_sponsor_name=_s(lead_sp.get("name")),
        lead_sponsor_class=_s(lead_sp.get("class")),
        responsible_party=responsible_party,
        collaborators=collaborators,
        oversight_has_dmc=bool(over_mod.get("oversightHasDmc", False)),
        is_fda_regulated_drug=bool(over_mod.get("isFdaRegulatedDrug", False)),
        is_fda_regulated_device=bool(over_mod.get("isFdaRegulatedDevice", False)),
        is_unapproved_device=bool(over_mod.get("isUnapprovedDevice", False)),
        is_ppsd=bool(over_mod.get("isPpsd", False)),
        fdaaa_801_violation=bool(over_mod.get("fdaaa801Violation", False)),
        brief_summary=_s(desc_mod.get("briefSummary")),
        detailed_description=_s(desc_mod.get("detailedDescription")),
        conditions=[c for c in (cond_mod.get("conditions") or []) if c],
        keywords=[k for k in (cond_mod.get("keywords") or []) if k],
        study_type=_s(design_mod.get("studyType")),
        phases=list(design_mod.get("phases") or []),
        enrollment_count=int(enrollment) if isinstance(enrollment, int) else None,
        enrollment_type=_s(enrollment_info.get("type")),
        design_info=design_info,
        bio_spec=bio_spec,
        target_duration=_s(design_mod.get("targetDuration")),
        expanded_access_types=[_s(t) for t in (design_mod.get("expandedAccessTypes") or []) if t],
        arm_groups=arm_groups,
        interventions=interventions,
        primary_outcomes=primary_outcomes,
        secondary_outcomes=secondary_outcomes,
        other_outcomes=other_outcomes,
        eligibility_criteria=_s(elig_mod.get("eligibilityCriteria")),
        healthy_volunteers=bool(elig_mod.get("healthyVolunteers", False)),
        sex=_s(elig_mod.get("sex")),
        gender_description=_s(elig_mod.get("genderDescription")),
        minimum_age=_s(elig_mod.get("minimumAge")),
        maximum_age=_s(elig_mod.get("maximumAge")),
        central_contacts=central_contacts,
        overall_officials=overall_officials,
        locations=locations,
        references=references,
        see_also_links=see_also_links,
        ipd_sharing=ipd_sharing,
        condition_browse_leaves=condition_browse_leaves,
        condition_browse_branches=condition_browse_branches,
        intervention_browse_leaves=intervention_browse_leaves,
        intervention_browse_branches=intervention_browse_branches,
        large_docs=large_docs,
        unposted_annotation=unposted_annotation,
        violation_annotation=violation_annotation,
        study_url=f"https://clinicaltrials.gov/study/{nct_id}" if nct_id else "",
    )


async def _fetch_ct_gov(condition: str, page_size: int, cursor: Optional[str]) -> tuple[list[Trial], Optional[str]]:
    params: dict[str, Any] = {
        "query.cond": condition,
        "filter.overallStatus": "RECRUITING",
        "pageSize": page_size,
        "format": "json",
    }
    if cursor:
        params["pageToken"] = cursor
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(CT_GOV_URL, params=params)
        r.raise_for_status()
        payload = r.json()
    studies = payload.get("studies") or []
    next_token = payload.get("nextPageToken")
    return [_parse_trial(s) for s in studies], next_token


async def _routes_api_one(client: httpx.AsyncClient, api_key: str, origin: str, dest: str) -> Optional[tuple[str, str]]:
    body = {
        "origin": {"address": origin},
        "destination": {"address": dest},
        "travelMode": "DRIVE",
        "routingPreference": "TRAFFIC_UNAWARE",
    }
    headers = {
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "routes.distanceMeters,routes.duration",
        "Content-Type": "application/json",
    }
    try:
        r = await client.post(ROUTES_API_URL, json=body, headers=headers, timeout=10.0)
        if r.status_code != 200:
            return None
        routes = (r.json() or {}).get("routes") or []
        if not routes:
            return None
        meters = routes[0].get("distanceMeters", 0)
        secs = int(str(routes[0].get("duration", "0s")).rstrip("s") or "0")
        miles = meters / 1609.34
        hrs, mins = secs // 3600, (secs % 3600) // 60
        return (f"{miles:.0f} miles", f"{hrs}h {mins}m" if hrs else f"{mins}m")
    except Exception:
        return None


async def _enrich_locations_with_travel(trials: list[Trial], user_location: str) -> None:
    api_key = os.environ.get(GOOGLE_MAPS_API_KEY_ENV, "")
    if not api_key or not user_location:
        return
    unique: dict[str, list[Location]] = {}
    for t in trials:
        for loc in t.locations:
            key = f"{loc.city}, {loc.state}".strip(", ")
            if key:
                unique.setdefault(key, []).append(loc)
    if not unique:
        return
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*[
            _routes_api_one(client, api_key, user_location, k) for k in unique.keys()
        ])
    for (key, locs), result in zip(unique.items(), results):
        if result is None:
            continue
        dist, dur = result
        for loc in locs:
            loc.distance = dist
            loc.duration = dur


def _enrich_npis(trials: list[Trial], code_map: dict[str, list[str]]) -> None:
    try:
        coll = providers_coll()
    except Exception:
        return
    for t in trials:
        derived = set(code_map.get(t.nct_id) or [])
        if not derived:
            continue
        for contact in t.central_contacts:
            name = (contact.name or "").strip()
            if not name:
                continue
            parts = name.split()
            if len(parts) < 2:
                continue
            first, last = parts[0], parts[-1]
            try:
                cursor = coll.find({
                    "provider_first_name": {"$regex": f"^{first}$", "$options": "i"},
                    "provider_last_name_legal_name": {"$regex": f"^{last}$", "$options": "i"},
                    "entity_type_code": "1",
                }, {"npi": 1, "taxonomies": 1, "_id": 0})
                matches = list(cursor)
            except Exception:
                continue
            relevant = [
                m for m in matches
                if any(
                    (tx.get("code") or "") in derived
                    for tx in (m.get("taxonomies") or [])
                )
            ]
            if len(relevant) == 1:
                contact.npi = relevant[0].get("npi") or None
        t.derived_nucc_codes = sorted(derived)


class ClinicalTrialsTool(ChatHealthyTool):
    """EPIC-006-F-031 — Find Clinical Trials."""
    TOOL_NAME = "clinical_trials"
    Request = Request
    Response = Response

    async def run(self, deps: AgentDeps, request: "Request") -> "Response":
        try:
            return await self._run_inner(deps, request)
        except Exception as exc:
            log.warning(
                "ClinicalTrialsTool caught exception; returning empty response: %s",
                exc,
                exc=ChatHealthyException(
                    mode="clinical_trials_tool_failed",
                    message=f"ClinicalTrialsTool caught exception: {exc}",
                    component="ClinicalTrialsTool",
                    exception=exc,
                ),
                if_not_debug_log=True,
            )
            return Response(
                trials=[],
                error=(
                    "I couldn't reach ClinicalTrials.gov right now. "
                    "Please try again in a minute."
                ),
            )

    async def _run_inner(self, deps: AgentDeps, request: "Request") -> "Response":
        condition = (request.condition or "").strip()
        if not condition:
            return Response(trials=[], error="No condition provided.")
        trials, next_cursor = await _fetch_ct_gov(
            condition, request.page_size, request.cursor,
        )
        if not trials:
            return Response(trials=[], cursor=None)

        trial_dicts = [
            {
                "nct_id": t.nct_id,
                "conditions": t.conditions,
                "brief_title": t.brief_title,
                "brief_summary": t.brief_summary,
            }
            for t in trials
        ]

        async def routes_branch() -> None:
            if request.user_location:
                await _enrich_locations_with_travel(trials, request.user_location)

        async def nucc_branch() -> dict[str, list[str]]:
            return await derive_nucc_codes_for_page(trial_dicts)

        _, code_map = await asyncio.gather(routes_branch(), nucc_branch())
        _enrich_npis(trials, code_map)

        result = Response(trials=trials, cursor=next_cursor)
        deps.stream({"kind": "trials", "data": result.model_dump(exclude_none=True)})
        return result


TOOL = ClinicalTrialsTool()
