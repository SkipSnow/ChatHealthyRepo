# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ProviderDetailService — EPIC-006-F-025 (data management + display).
#
# Flow per design v3:
#   1. Fetch the full live NPPES record by NPI.
#   2. Compare against the stored provider record in Mongo.
#   3. On divergence, write the live values back into the stored record
#      preserving / re-running pipeline enrichments per the matrix.
#   4. Stamp provenance sidecar; embedding runs as a BackgroundTask
#      scheduled by the FastAPI handler after the response returns.
#   5. Return a projection of the (current) stored record as npi_details
#      so the front-end's existing render path works unchanged.

import logging
import urllib.parse

import requests

from domain.find_care import provider_record_sync as _sync

_log = logging.getLogger("findcare.provider_detail")

STATE_BOARDS = {
    "DE": ("https://dpr.delaware.gov/boardsearch/", "Delaware"),
    "MS": ("https://www.msbml.ms.gov/licensure", "Mississippi"),
    "VA": ("https://dhp.virginiainteractive.org/Lookup/Index", "Virginia"),
}


def _stored_to_npi_details(stored: dict) -> dict:
    """Project the Mongo provider record into the legacy npi_details flat
    shape the front-end's render path consumes. Per the design's MVP
    boundary: the back-end produces fresh truth via the write-back; the
    front-end keeps its current shape."""
    primary_addr = next(
        (a for a in stored.get("addresses") or []
         if a.get("address_type") == "practice"),
        (stored.get("addresses") or [{}])[0] if stored.get("addresses") else {},
    )
    primary_tax = next(
        (t for t in stored.get("taxonomies") or [] if t.get("primary")),
        (stored.get("taxonomies") or [{}])[0] if stored.get("taxonomies") else {},
    )
    licenses = stored.get("licenses") or []
    primary_license = licenses[0] if licenses else {}
    name = " ".join(
        s for s in (
            stored.get("provider_name_prefix_text", ""),
            stored.get("provider_first_name", ""),
            stored.get("provider_middle_name", ""),
            stored.get("provider_last_name_legal_name", ""),
        ) if s
    )
    addr_parts = (
        primary_addr.get("line1", ""),
        primary_addr.get("city", ""),
        primary_addr.get("state", ""),
        (primary_addr.get("zip") or "")[:5],
    )
    return {
        "name": name,
        "npi": stored.get("npi", ""),
        "status": (
            "A" if (stored.get("active") or [{}])[-1].get(
                "is_active", True
            ) else "I"
        ),
        "credential": stored.get("provider_credential_text", ""),
        "specialty": primary_tax.get("code", ""),
        "license": primary_license.get("number", ""),
        "license_state": primary_license.get("state", ""),
        "address": ", ".join(s for s in addr_parts if s),
        "phone": primary_addr.get("phone", ""),
        "enumeration_date": stored.get(
            "provider_enumeration_date", ""
        ),
    }


class ProviderDetailService:
    """Provider detail lookup with compare-and-write-back cycle.

    lookup() takes the bound provider collection + a callback to schedule
    the re-embed BackgroundTask. The handler injects both at call time
    so the binding is resolved AFTER bind_from_manifest runs.
    """

    def lookup(
        self,
        provider_name: str,
        npi: str = "",
        state: str = "",
        provider_coll=None,
        schedule_background_task=None,
        **kwargs,
    ) -> dict:
        """Run the four-step cycle (per design v3). When the cycle runs
        successfully and schedule_background_task is supplied, the
        re-embed task is scheduled for after the response returns.

        Returns the same shape as before {provider_name, npi,
        research_sites, npi_details?}, where npi_details is derived
        from the (current) stored record.
        """
        name_q = urllib.parse.quote_plus(provider_name)
        npi_details = None
        sync_summary = None

        if npi and provider_coll is not None:
            try:
                npi_details, sync_summary = self._sync_cycle(
                    npi, provider_coll,
                )
            except Exception as exc:
                _log.warning(
                    "provider_detail sync cycle failed for NPI %s: %s",
                    npi, exc,
                )
        elif npi:
            npi_details = self._legacy_direct_lookup(npi)

        if sync_summary is not None:
            _log.info(
                "provider_detail sync NPI=%s divergence=%s "
                "new_addresses_resolved=%d google_maps_calls=%d "
                "google_maps_failures=%d",
                npi,
                sync_summary["divergence"],
                sync_summary["new_addresses_resolved"],
                sync_summary["google_maps_calls"],
                sync_summary["google_maps_failures"],
            )
            # REQ-B-014: re-embed runs ONLY after a successful write-back,
            # not on every cycle.
            if (
                sync_summary["divergence"]
                and schedule_background_task is not None
            ):
                schedule_background_task(
                    _sync.embed_after_response, provider_coll, npi,
                )

        research_sites = self._build_research_sites(name_q, npi, state)
        result = {
            "provider_name": provider_name,
            "npi": npi,
            "research_sites": research_sites,
        }
        if npi_details:
            result["npi_details"] = npi_details
        return result

    def _sync_cycle(self, npi: str, coll):
        live_record = self._fetch_live(npi)
        if live_record is None:
            stored = coll.find_one({"npi": npi})
            if stored is None:
                return None, None
            return _stored_to_npi_details(stored), None

        stored = coll.find_one({"npi": npi})
        if stored is None:
            return None, None

        live_proj = _sync.live_to_comparable(live_record)
        stored_proj = _sync.stored_to_comparable(stored)
        divergence = _sync.compare(live_proj, stored_proj)

        sync_summary = {
            "divergence": _sync.has_any_divergence(divergence),
            "new_addresses_resolved": 0,
            "google_maps_calls": 0,
            "google_maps_failures": 0,
        }

        if sync_summary["divergence"]:
            new_doc = _sync.merge_for_writeback(live_record, stored)
            stored_addr_keys = {
                (
                    (a.get("line1") or "").strip(),
                    (a.get("city") or "").strip(),
                    (a.get("state") or "").strip(),
                    (a.get("zip") or "")[:5],
                )
                for a in stored.get("addresses") or []
            }
            for a in new_doc.get("addresses") or []:
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
            _sync.write_back(coll, npi, new_doc)
            stored = coll.find_one({"npi": npi})

        return _stored_to_npi_details(stored), sync_summary

    def _fetch_live(self, npi: str) -> dict | None:
        try:
            resp = requests.get(
                f"https://npiregistry.cms.hhs.gov/api/?number={npi}&version=2.1",
                timeout=10,
            )
        except Exception as exc:
            _log.warning("NPPES live fetch failed for NPI %s: %s", npi, exc)
            return None
        if resp.status_code != 200:
            _log.warning(
                "NPPES live fetch non-200 for NPI %s: %d",
                npi, resp.status_code,
            )
            return None
        try:
            data = resp.json()
        except Exception as exc:
            _log.warning(
                "NPPES live fetch malformed JSON for NPI %s: %s",
                npi, exc,
            )
            return None
        results = data.get("results") or []
        if not results:
            _log.warning("NPPES live fetch zero results for NPI %s", npi)
            return None
        return results[0]

    def _legacy_direct_lookup(self, npi: str) -> dict | None:
        """Pre-sync-cycle behavior preserved for the no-collection case."""
        try:
            resp = requests.get(
                f"https://npiregistry.cms.hhs.gov/api/?number={npi}&version=2.1",
                timeout=10,
            )
            if resp.status_code != 200:
                return None
            results = resp.json().get("results", [])
            if not results:
                return None
            r = results[0]
            basic = r.get("basic", {})
            addrs = [a for a in r.get("addresses", [])
                     if a.get("address_purpose") == "LOCATION"]
            addr = addrs[0] if addrs else {}
            taxonomies = r.get("taxonomies", [])
            primary_tax = next(
                (t for t in taxonomies if t.get("primary")),
                taxonomies[0] if taxonomies else {},
            )
            return {
                "name": " ".join(filter(None, [
                    basic.get("name_prefix", ""),
                    basic.get("first_name", ""),
                    basic.get("middle_name", ""),
                    basic.get("last_name", ""),
                ])),
                "npi": r.get("number", npi),
                "status": basic.get("status", ""),
                "credential": basic.get("credential", ""),
                "specialty": primary_tax.get("desc", ""),
                "license": primary_tax.get("license", ""),
                "license_state": primary_tax.get("state", ""),
                "address": ", ".join(filter(None, [
                    addr.get("address_1", ""),
                    addr.get("city", ""),
                    addr.get("state", ""),
                    addr.get("postal_code", "")[:5]
                    if addr.get("postal_code") else "",
                ])),
                "phone": addr.get("telephone_number", ""),
                "enumeration_date": basic.get("enumeration_date", ""),
            }
        except Exception as exc:
            _log.warning("NPI Registry legacy lookup failed for %s: %s", npi, exc)
            return None

    def _build_research_sites(
        self, name_q: str, npi: str, state: str,
    ) -> dict:
        sites = {
            "healthgrades": {
                "url": (
                    f"https://www.healthgrades.com/find-a-doctor?"
                    f"what={name_q}&where={state}"
                ),
                "name": "Healthgrades",
                "guidance": (
                    "Search by the provider's full name and state. "
                    "Note: Healthgrades uses fuzzy matching and may show "
                    "similar names — verify the provider name and address "
                    "match before relying on reviews."
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
                    "The official federal registry. Search by NPI number "
                    "for exact match. Shows license state, specialty, and "
                    "practice address. This is the most reliable source "
                    "for verifying a provider's credentials."
                ),
            },
        }
        state_upper = state.upper()
        if state_upper in STATE_BOARDS:
            board_url, state_name = STATE_BOARDS[state_upper]
            sites["state_medical_board"] = {
                "url": board_url,
                "name": f"{state_name} Medical Board",
                "guidance": (
                    f"Search the {state_name} state medical board to "
                    "verify active licensure, check for disciplinary "
                    "actions, and confirm board certification."
                ),
            }
        return sites
