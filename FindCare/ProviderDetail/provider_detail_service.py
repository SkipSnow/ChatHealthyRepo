# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ProviderDetailService — EPIC-006-F-002 (data management + display).
#
# Flow per S-002:
#   1. Fetch the full live NPPES record by NPI.
#   2. Compare against the stored provider record in Mongo.
#   3. On divergence, write the live values back into the stored record
#      preserving / re-running pipeline enrichments per the matrix.
#   4. Project the (current) stored record into the display payload via
#      ProviderDetailOutput.from_stored — each display type owns its
#      conversion next to its definition.
#   5. Embedding runs as a BackgroundTask scheduled by the FastAPI
#      handler after the response returns.

from typing import Optional

from chathealthy_lib import ChatHealthyLoggingService
from chathealthy_lib.exceptions import ChatHealthyException
import urllib.parse

import requests

from domain.find_care import provider_record_sync
from .provider_detail_models import ProviderDetailOutput

log = ChatHealthyLoggingService()


STATE_BOARDS = {
    "AK": ("https://www.commerce.alaska.gov/cbp/main/search/professional", "Alaska State Medical Board"),
    "AL": ("https://www.albme.gov/consumers/licensee-search/", "Alabama Board of Medical Examiners & Medical Licensure Commission"),
    "AR": ("https://armedicalboard.adh.arkansas.gov/public/verify/default.aspx", "Arkansas State Medical Board"),
    "AZ": ("https://azbomprod.azmd.gov/glsuiteweb/clients/azbom/public/webverificationsearch.aspx", "Arizona Medical Board"),
    "CA": ("https://www.mbc.ca.gov/Breeze/License_Verification.aspx", "Medical Board of California"),
    "CO": ("https://apps.colorado.gov/dora/licensing/Lookup/LicenseLookup.aspx", "Colorado Medical Board"),
    "CT": ("https://www.elicense.ct.gov/Lookup/LicenseLookup.aspx", "Connecticut Medical Examining Board"),
    "DC": ("https://app.hpla.doh.dc.gov/Weblookupcs/", "District of Columbia Board of Medicine"),
    "DE": ("https://delpros.delaware.gov/oh_verifylicense", "Delaware Board of Medical Licensure and Discipline"),
    "FL": ("https://mqa-internet.doh.state.fl.us/MQASearchServices/HealthCareProviders", "Florida Board of Medicine"),
    "GA": ("https://gateway.medicalboard.georgia.gov/verification/search.aspx", "Georgia Composite Medical Board"),
    "HI": ("https://mypvl.dcca.hawaii.gov/public-license-search/", "Hawaii Medical Board"),
    "IA": ("https://amanda-portal.idph.state.ia.us/ibm/portal/", "Iowa Board of Medicine"),
    "ID": ("https://apps-dopl.idaho.gov/IBOMPortal/AgencyAdditional.aspx?Agency=425&AgencyLinkID=570", "Idaho Board of Medicine"),
    "IL": ("https://online-dfpr.micropact.com/lookup/licenselookup.aspx", "Illinois Department of Financial and Professional Regulation"),
    "IN": ("https://mylicense.in.gov/everification/", "Medical Licensing Board of Indiana"),
    "KS": ("https://www.kansas.gov/ssrv-ksbhada/search.html", "Kansas State Board of Healing Arts"),
    "KY": ("https://kbml.ky.gov/physician/Pages/Physician-Profile-Verification-of-Physician-License.aspx", "Kentucky Board of Medical Licensure"),
    "LA": ("https://apps.lsbme.la.gov/verifications/results.aspx", "Louisiana State Board of Medical Examiners"),
    "MA": ("https://www.mass.gov/how-to/verify-a-physician-or-acupuncture-license", "Massachusetts Board of Registration in Medicine"),
    "MD": ("https://www.mbp.state.md.us/mbp_ah/verification.aspx", "Maryland Board of Physicians"),
    "ME": ("https://www.maine.gov/md/licensure/license-verification", "Maine Board of Licensure in Medicine"),
    "MI": ("https://www.michigan.gov/lara/i-need-to/find-or-verify-a-licensed-professional-or-business", "Michigan Board of Medicine"),
    "MN": ("https://mn.gov/boards/medical-practice/verify/index.jsp", "Minnesota Board of Medical Practice"),
    "MO": ("https://pr.mo.gov/licensee-search.asp", "Missouri Board of Registration for the Healing Arts"),
    "MS": ("https://gateway.msbml.ms.gov/verification/search.aspx", "Mississippi State Board of Medical Licensure"),
    "MT": ("https://ebizws.mt.gov/PUBLICPORTAL/searchform?mylist=licenses", "Montana Board of Medical Examiners"),
    "NC": ("https://portal.ncmedboard.org/verification/search.aspx", "North Carolina Medical Board"),
    "ND": ("https://www.ndbom.org/public/find_verify/verify.asp", "North Dakota Board of Medicine"),
    "NE": ("https://www.nebraska.gov/LISSearch/search.cgi", "Nebraska Board of Medicine and Surgery"),
    "NH": ("https://www.oplc.nh.gov/license-lookup", "New Hampshire Board of Medicine"),
    "NJ": ("https://newjersey.mylicense.com/verification/", "New Jersey Division of Consumer Affairs — Board of Medical Examiners"),
    "NM": ("https://nmrldlpi.my.site.com/nmmb/s/searchlicense", "New Mexico Medical Board"),
    "NV": ("https://nsbme.us.thentiacloud.net/webs/nsbme/register/", "Nevada State Board of Medical Examiners"),
    "NY": ("https://www.op.nysed.gov/services/verifications/online-verification-searches", "New York State Board for Medicine"),
    "OH": ("https://elicense.ohio.gov/oh_verifylicense", "State Medical Board of Ohio"),
    "OK": ("https://www.okmedicalboard.org/search", "Oklahoma State Board of Medical Licensure and Supervision"),
    "OR": ("https://omb.oregon.gov/clients/ormb/public/verification.aspx", "Oregon Medical Board"),
    "PA": ("https://www.pals.pa.gov/#/page/search", "Pennsylvania State Board of Medicine"),
    "PR": ("https://orcps.salud.pr.gov/mbps/verificacion", "Junta de Licenciamiento y Disciplina Médica de Puerto Rico"),
    "RI": ("https://health.ri.gov/find/licensees/index.php", "Rhode Island Board of Medical Licensure and Discipline"),
    "SC": ("https://verify.llronline.com/", "South Carolina Department of Labor, Licensing and Regulation — Board of Medical Examiners"),
    "SD": ("https://www.sdbmoe.gov/sdbmoe-licensee-lookup/", "South Dakota Board of Medical and Osteopathic Examiners"),
    "TN": ("https://apps.health.tn.gov/Licensure/", "Tennessee Board of Medical Examiners"),
    "TX": ("https://www.tmb.texas.gov/resources/for-the-public/look-up-a-license", "Texas Medical Board"),
    "UT": ("https://verify.commerce.utah.gov/verify/Search.aspx", "Utah Physicians Licensing Board"),
    "VA": ("https://dhp.virginiainteractive.org/lookup/index", "Virginia Board of Medicine"),
    "VT": ("https://mpb.health.vermont.gov/Lookup/LicenseLookup.aspx", "Vermont Board of Medical Practice"),
    "WA": ("https://wmc.wa.gov/licensing/verification-requests", "Washington Medical Commission"),
    "WI": ("https://licensesearch.wi.gov/", "Wisconsin Medical Examining Board"),
    "WV": ("https://wvbom.wv.gov/public/search/index.asp", "West Virginia Board of Medicine"),
    "WY": ("https://wyomedboard.wyo.gov/consumers/license-lookup", "Wyoming Board of Medicine"),
}


def resolve_taxonomy_display_names(
    codes: list[str],
    specialty_meta_coll_callable,
) -> dict[str, str]:
    """Join NUCC codes against SpecialtyMetaData to get display names."""
    if not codes or specialty_meta_coll_callable is None:
        return {}
    try:
        coll = specialty_meta_coll_callable()
        docs = coll.find(
            {"Code": {"$in": list(set(codes))}},
            {"Code": 1, "Display Name": 1, "_id": 0},
        )
        return {
            (d.get("Code") or ""): (d.get("Display Name") or "")
            for d in docs
        }
    except Exception as exc:
        # Mode 1 (REQ-B-008): taxonomy display name lookup; if it fails,
        # the detail panel renders without specialty display names (codes
        # still show). Graceful, non-blocking degradation.
        log.info(
            "taxonomy display name lookup failed: %s", exc,
            exc=ChatHealthyException(
                mode="taxonomy_display_name_lookup_failed",
                message=f"taxonomy display name lookup failed: {exc}",
                component="ProviderDetailService",
                exception=exc,
            ),
        )
        return {}


def _every_address(record: dict) -> list[dict]:
    """Every address on a record, practice first then business.

    v4 splits the single addresses[] into practice_addresses[] and a lone
    business_address. One helper so every caller reads the two fields the
    same way, rather than each remembering to look in both places.
    """
    out = list(record.get("practice_addresses") or [])
    business = record.get("business_address")
    if business:
        out.append(business)
    return out


class ProviderDetailService:
    """Provider detail lookup with compare-and-write-back cycle."""

    @staticmethod
    def display_name(stored: Optional[dict]) -> str:
        """The provider's name as the card writes it, read from the record.

        Same parts Identity.from_stored uses, so the header and the
        identity block below it cannot say different things.
        """
        if not stored:
            return ""
        parts = [
            (stored.get("provider_first_name") or "").strip(),
            (stored.get("provider_middle_name") or "").strip(),
            (stored.get("provider_last_name_legal_name") or "").strip(),
        ]
        name = " ".join(p for p in parts if p)
        credential = (stored.get("provider_credential_text") or "").strip()
        if name and credential:
            return f"{name}, {credential}"
        return name or credential

    def lookup(
        self,
        provider_name: str = "",
        npi: str = "",
        state: str = "",
        provider_coll=None,
        specialty_meta_coll=None,
        schedule_background_task=None,
        **kwargs,
    ) -> dict:
        stored = None
        sync_summary = None

        if npi and provider_coll is not None:
            try:
                stored, sync_summary = self.sync_cycle(npi, provider_coll)
            except Exception as exc:
                # Mode 2 (REQ-B-008): the live-vs-stored sync cycle (the
                # F-025-S-002 reconciliation) failed; stored stays None and
                # the user sees an empty detail panel. Operator MUST know —
                # the live-refresh contract is the headline feature of
                # provider detail.
                log.error(
                    "provider_detail sync cycle failed for NPI %s: %s",
                    npi, exc,
                    exc=ChatHealthyException(
                        mode="provider_detail_sync_cycle_failed",
                        message=f"provider_detail sync cycle failed for NPI {npi}: {exc}",
                        component="ProviderDetailService",
                        exception=exc,
                    ),
                    if_not_debug_log=True,
                )

        if sync_summary is not None:
            log.info(
                "provider_detail sync NPI=%s divergence=%s "
                "new_addresses_resolved=%d google_maps_calls=%d "
                "google_maps_failures=%d",
                npi,
                sync_summary["divergence"],
                sync_summary["new_addresses_resolved"],
                sync_summary["google_maps_calls"],
                sync_summary["google_maps_failures"],
            )
            if (
                sync_summary["embedding_text_changed"]
                and schedule_background_task is not None
            ):
                schedule_background_task(
                    provider_record_sync.embed_after_response, provider_coll, npi,
                )

        if stored is not None:
            codes = [
                (t.get("code") or "").strip()
                for t in stored.get("taxonomies") or []
            ]
            code_to_display = resolve_taxonomy_display_names(
                codes, specialty_meta_coll,
            )
            primary_code = ProviderDetailOutput.primary_taxonomy_code(stored)
            primary_display = code_to_display.get(primary_code, "")
            primary_state = (
                ProviderDetailOutput.primary_practice_state(stored)
                or (state or "").upper()
            )
        else:
            code_to_display = {}
            primary_display = ""
            primary_state = (state or "").upper()

        # A detail opened from a recorded selection carries the NPI and no
        # card, so the name comes from the record -- which is where it
        # should come from anyway. Every other name field on the panel is
        # already read from `stored`; taking the header and the research
        # links from a card that may have been painted some time ago is how
        # they would disagree with the rest of the panel.
        provider_name = provider_name or self.display_name(stored)

        research_sites = self.build_research_sites(
            provider_name=provider_name,
            npi=npi,
            state=primary_state,
        )

        return ProviderDetailOutput.from_stored(
            provider_name=provider_name,
            npi=npi,
            stored=stored,
            code_to_display=code_to_display,
            primary_taxonomy_display=primary_display,
            research_sites=research_sites,
        )

    def sync_cycle(self, npi: str, coll):
        """Returns (stored_record, sync_summary). stored_record is the
        post-write provider record; sync_summary is None when live fetch
        failed (no comparison ran)."""
        live_record = self.fetch_live(npi)
        if live_record is None:
            stored = coll.find_one({"npi": npi})
            return stored, None

        stored = coll.find_one({"npi": npi})
        if stored is None:
            return None, None

        live_proj = provider_record_sync.live_to_comparable(live_record)
        stored_proj = provider_record_sync.stored_to_comparable(stored)
        divergence = provider_record_sync.compare(live_proj, stored_proj)

        sync_summary = {
            "divergence": provider_record_sync.has_any_divergence(divergence),
            "embedding_text_changed": False,
            "new_addresses_resolved": 0,
            "google_maps_calls": 0,
            "google_maps_failures": 0,
        }

        if sync_summary["divergence"]:
            new_doc = provider_record_sync.merge_for_writeback(live_record, stored)
            sync_summary["embedding_text_changed"] = (
                provider_record_sync.build_embedding_text(new_doc)
                != provider_record_sync.build_embedding_text(stored)
            )
            stored_addr_keys = {
                (
                    (a.get("line1") or "").strip(),
                    (a.get("city") or "").strip(),
                    (a.get("state") or "").strip(),
                    (a.get("zip") or "")[:5],
                )
                for a in _every_address(stored)
            }
            for a in _every_address(new_doc):
                key = (
                    a.get("line1", ""),
                    a.get("city", ""),
                    a.get("state", ""),
                    a.get("zip", ""),
                )
                if key not in stored_addr_keys:
                    sync_summary["new_addresses_resolved"] += 1
                    sync_summary["google_maps_calls"] += 1
                    if not (a.get("county") or {}).get("name"):
                        sync_summary["google_maps_failures"] += 1
            provider_record_sync.write_back(coll, npi, new_doc)
            stored = coll.find_one({"npi": npi})

        return stored, sync_summary

    def fetch_live(self, npi: str) -> dict | None:
        try:
            resp = requests.get(
                f"https://npiregistry.cms.hhs.gov/api/?number={npi}&version=2.1",
                timeout=10,
            )
        except Exception as exc:
            # Mode 2 (REQ-B-008): NPPES live fetch failed (network/timeout);
            # we fall back to the stored record without a reconciliation
            # pass — user sees potentially stale data. NPPES is a critical
            # data source; operator MUST know about any outage.
            log.error(
                "NPPES live fetch failed for NPI %s: %s", npi, exc,
                exc=ChatHealthyException(
                    mode="nppes_fetch_failed",
                    message=f"NPPES live fetch failed for NPI {npi}: {exc}",
                    component="ProviderDetailService",
                    exception=exc,
                ),
                if_not_debug_log=True,
            )
            return None
        if resp.status_code != 200:
            log.warning(
                "NPPES live fetch non-200 for NPI %s: %d",
                npi, resp.status_code,
            )
            return None
        try:
            data = resp.json()
        except Exception as exc:
            # Mode 2 (REQ-B-008): NPPES returned non-JSON; we fall back to
            # the stored record without reconciliation. NPPES API contract
            # break — operator MUST know.
            log.error(
                "NPPES live fetch malformed JSON for NPI %s: %s",
                npi, exc,
                exc=ChatHealthyException(
                    mode="nppes_fetch_malformed_json",
                    message=f"NPPES live fetch malformed JSON for NPI {npi}: {exc}",
                    component="ProviderDetailService",
                    exception=exc,
                ),
                if_not_debug_log=True,
            )
            return None
        results = data.get("results") or []
        if not results:
            log.warning("NPPES live fetch zero results for NPI %s", npi)
            return None
        return results[0]

    def build_research_sites(
        self,
        provider_name: str,
        npi: str,
        state: str,
    ) -> dict:
        name_q = urllib.parse.quote_plus(provider_name or "")
        sites = {
            "healthgrades": {
                "url": (
                    f"https://www.healthgrades.com/find-a-doctor?"
                    f"what={name_q}&where={state}"
                ),
                "name": "Healthgrades",
                "guidance": (
                    "Search by the provider's full name and state. "
                    "Healthgrades uses fuzzy matching and may show "
                    "similar names — verify the provider name and "
                    "address match before relying on reviews."
                ),
            },
            "npi_registry": {
                "url": (
                    f"https://npiregistry.cms.hhs.gov/search?number={npi}"
                    if npi
                    else f"https://npiregistry.cms.hhs.gov/search?"
                         f"name_type=ind&first_name={name_q}"
                ),
                "name": "NPI Registry (CMS)",
                "guidance": (
                    "The official federal registry. Search by NPI "
                    "number for exact match. Shows license state, "
                    "specialty, and practice address. This is the "
                    "most reliable source for verifying a provider's "
                    "credentials."
                ),
            },
        }
        state_upper = (state or "").upper()
        if state_upper in STATE_BOARDS:
            board_url, board_name = STATE_BOARDS[state_upper]
            sites["state_medical_board"] = {
                "url": board_url,
                "name": board_name,
                "guidance": (
                    f"Verify active licensure with the {board_name}. "
                    "Confirm board certification and check for "
                    "disciplinary actions."
                ),
            }
        return sites
