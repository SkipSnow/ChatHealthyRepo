# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# ProviderDetailService — UAT Feature 8: Provider Detail (NPI lookup + external links)
#
# Extracted from main.py as part of ARCH-001 Phase 4.
# Business component: EvaluateCareQuality (evaluating provider quality, not finding them)
#
# Design: ARCH-001

import logging
import urllib.parse

import requests

_log = logging.getLogger("findcare.provider_detail")

STATE_BOARDS = {
    "DE": ("https://dpr.delaware.gov/boardsearch/", "Delaware"),
    "MS": ("https://www.msbml.ms.gov/licensure", "Mississippi"),
    "VA": ("https://dhp.virginiainteractive.org/Lookup/Index", "Virginia"),
}


class ProviderDetailService:
    """Provider detail lookup: NPI Registry + external research links.

    Dependencies: NPI Registry API (CMS), no database needed.
    """

    def lookup(self, provider_name: str, npi: str = "", state: str = "", **kwargs) -> dict:
        """Look up provider details from NPI Registry and construct research URLs."""
        name_q = urllib.parse.quote_plus(provider_name)

        npi_data = None
        if npi:
            try:
                npi_resp = requests.get(
                    f"https://npiregistry.cms.hhs.gov/api/?number={npi}&version=2.1",
                    timeout=10,
                )
                if npi_resp.status_code == 200:
                    results = npi_resp.json().get("results", [])
                    if results:
                        r = results[0]
                        basic = r.get("basic", {})
                        addrs = [a for a in r.get("addresses", []) if a.get("address_purpose") == "LOCATION"]
                        addr = addrs[0] if addrs else {}
                        taxonomies = r.get("taxonomies", [])
                        primary_tax = next((t for t in taxonomies if t.get("primary")), taxonomies[0] if taxonomies else {})
                        npi_data = {
                            "name": " ".join(filter(None, [basic.get("name_prefix", ""), basic.get("first_name", ""), basic.get("middle_name", ""), basic.get("last_name", "")])),
                            "npi": r.get("number", npi),
                            "status": basic.get("status", ""),
                            "credential": basic.get("credential", ""),
                            "specialty": primary_tax.get("desc", ""),
                            "license": primary_tax.get("license", ""),
                            "license_state": primary_tax.get("state", ""),
                            "address": ", ".join(filter(None, [addr.get("address_1", ""), addr.get("city", ""), addr.get("state", ""), addr.get("postal_code", "")[:5] if addr.get("postal_code") else ""])),
                            "phone": addr.get("telephone_number", ""),
                            "enumeration_date": basic.get("enumeration_date", ""),
                        }
            except Exception as exc:
                _log.warning("NPI Registry lookup failed for %s: %s", npi, exc)

        research_sites = {
            "healthgrades": {
                "url": f"https://www.healthgrades.com/find-a-doctor?what={name_q}&where={state}",
                "name": "Healthgrades",
                "guidance": "Search by the provider's full name and state. Note: Healthgrades uses fuzzy matching and may show similar names — verify the provider name and address match before relying on reviews.",
            },
            "npi_registry": {
                "url": f"https://npiregistry.cms.hhs.gov/search?number={npi}" if npi else f"https://npiregistry.cms.hhs.gov/search?name_type=ind&first_name={name_q}",
                "name": "NPI Registry (CMS)",
                "guidance": "The official federal registry. Search by NPI number for exact match. Shows license state, specialty, and practice address. This is the most reliable source for verifying a provider's credentials.",
            },
        }

        state_upper = state.upper()
        if state_upper in STATE_BOARDS:
            board_url, state_name = STATE_BOARDS[state_upper]
            research_sites["state_medical_board"] = {
                "url": board_url,
                "name": f"{state_name} Medical Board",
                "guidance": f"Search the {state_name} state medical board to verify active licensure, check for disciplinary actions, and confirm board certification.",
            }

        result = {"provider_name": provider_name, "npi": npi, "research_sites": research_sites}
        if npi_data:
            result["npi_details"] = npi_data
        return result
