# Copyright (c) 2026 Skip Snow. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
#
# county_economic_enrichment.py — Enrich provider county data with economic indicators.
#
# Adds to the existing county attribute on provider records:
#   - gdp: county-level GDP (BEA CAGDP2)
#   - mean_income: mean household income (Census ACS B19025)
#   - median_income: median household income (Census ACS B19013)
#   - rural_urban_code: USDA Rural-Urban Continuum Code (1-9)
#   - rural_urban_label: metro/nonmetro/rural classification
#
# Data sources:
#   - BEA: apps.bea.gov/api (CAGDP2 table, county GDP by FIPS)
#   - Census: api.census.gov (ACS 5-year, tables B19013, B19025)
#   - USDA: ers.usda.gov (Rural-Urban Continuum Codes CSV)
#
# Requires: BEA_API_KEY, CENSUS_API_KEY environment variables.
# USDA RUCC data downloaded as CSV (no API key needed).

import csv
import io
import json
import logging
import os
import urllib.request
from typing import Optional

_log = logging.getLogger("pipeline.county_economic")

# USDA Rural-Urban Continuum Codes
# 1-3 = Metro, 4-6 = Nonmetro adjacent, 7-9 = Nonmetro nonadjacent (rural)
RUCC_LABELS = {
    1: "Metro (1M+)",
    2: "Metro (250K-1M)",
    3: "Metro (<250K)",
    4: "Nonmetro (urban 20K+, adjacent)",
    5: "Nonmetro (urban 20K+, nonadjacent)",
    6: "Nonmetro (urban 2.5K-19.9K, adjacent)",
    7: "Nonmetro (urban 2.5K-19.9K, nonadjacent)",
    8: "Rural (adjacent, <2.5K urban)",
    9: "Rural (nonadjacent, <2.5K urban)",
}


def _fetch_json(url: str, timeout: int = 30) -> dict:
    """Fetch JSON from URL."""
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_csv(url: str, timeout: int = 30) -> list[dict]:
    """Fetch CSV from URL, return list of dicts."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def fetch_census_income(state_fips: str, api_key: str) -> dict:
    """Fetch median and mean household income by county from Census ACS 5-year.

    Returns {fips_code: {median_income, mean_income}}.
    B19013_001E = median household income
    B19025_001E = mean household income (aggregate / households)
    """
    url = (
        f"https://api.census.gov/data/2022/acs/acs5"
        f"?get=NAME,B19013_001E,B19025_001E"
        f"&for=county:*"
        f"&in=state:{state_fips}"
        f"&key={api_key}"
    )
    try:
        data = _fetch_json(url)
        # First row is headers: ['NAME', 'B19013_001E', 'B19025_001E', 'state', 'county']
        results = {}
        for row in data[1:]:
            fips = row[3] + row[4]  # state FIPS + county FIPS
            median = int(row[1]) if row[1] and row[1] != "null" else None
            mean = int(row[2]) if row[2] and row[2] != "null" else None
            results[fips] = {"median_income": median, "mean_income": mean}
        _log.info("Census income: %d counties for state %s", len(results), state_fips)
        return results
    except Exception as e:
        _log.error("Census income fetch failed for state %s: %s", state_fips, e)
        return {}


def fetch_bea_gdp(state_fips: str, api_key: str) -> dict:
    """Fetch county-level GDP from BEA CAGDP2 table.

    Returns {fips_code: gdp_millions}.
    """
    url = (
        f"https://apps.bea.gov/api/data/"
        f"?UserID={api_key}"
        f"&method=GetData"
        f"&DataSetName=CAGDP2"
        f"&GeoFips=COUNTY"
        f"&LineCode=1"  # All industry total
        f"&Year=LAST5"
        f"&ResultFormat=JSON"
    )
    try:
        data = _fetch_json(url, timeout=60)
        results_data = data.get("BEAAPI", {}).get("Results", {}).get("Data", [])
        # Get most recent year per county
        gdp_by_fips = {}
        for row in results_data:
            fips = row.get("GeoFips", "").replace('"', '')
            if len(fips) == 5 and fips.startswith(state_fips):
                try:
                    val = float(row.get("DataValue", "0").replace(",", ""))
                    year = int(row.get("TimePeriod", "0"))
                    if fips not in gdp_by_fips or year > gdp_by_fips[fips]["year"]:
                        gdp_by_fips[fips] = {"gdp_millions": val, "year": year}
                except (ValueError, TypeError):
                    pass
        results = {fips: d["gdp_millions"] for fips, d in gdp_by_fips.items()}
        _log.info("BEA GDP: %d counties for state %s", len(results), state_fips)
        return results
    except Exception as e:
        _log.error("BEA GDP fetch failed for state %s: %s", state_fips, e)
        return {}


def fetch_rucc() -> dict:
    """Fetch USDA Rural-Urban Continuum Codes.

    Returns {fips_code: {code: int, label: str}}.
    """
    url = "https://www.ers.usda.gov/webdocs/DataFiles/53251/ruralurbancodes2023.csv"
    try:
        rows = _fetch_csv(url)
        results = {}
        for row in rows:
            fips = row.get("FIPS", "").zfill(5)
            code = int(row.get("RUCC_2023", row.get("RUCC_2013", "0")))
            results[fips] = {
                "code": code,
                "label": RUCC_LABELS.get(code, "Unknown"),
                "is_rural": code >= 7,
                "is_metro": code <= 3,
            }
        _log.info("RUCC: %d counties loaded", len(results))
        return results
    except Exception as e:
        _log.error("RUCC fetch failed: %s", e)
        return {}


# State FIPS codes for our supported states
STATE_FIPS = {
    "DE": "10",
    "MS": "28",
    "VA": "51",
}


def build_county_economics(states: list[str] = None) -> dict:
    """Build complete county economic data for supported states.

    Returns {fips_code: {gdp_millions, median_income, mean_income, rural_urban_code, rural_urban_label, is_rural, is_metro}}.
    """
    if states is None:
        states = list(STATE_FIPS.keys())

    census_key = os.getenv("CENSUS_API_KEY", "")
    bea_key = os.getenv("BEA_API_KEY", "")

    # RUCC is national — one fetch
    rucc = fetch_rucc()

    all_economics = {}
    for state in states:
        fips_prefix = STATE_FIPS.get(state)
        if not fips_prefix:
            _log.warning("No FIPS prefix for state %s, skipping", state)
            continue

        income = fetch_census_income(fips_prefix, census_key) if census_key else {}
        gdp = fetch_bea_gdp(fips_prefix, bea_key) if bea_key else {}

        # Merge by FIPS
        county_fips = set(list(income.keys()) + list(gdp.keys()) + [k for k in rucc if k.startswith(fips_prefix)])
        for fips in county_fips:
            economics = {
                "gdp_millions": gdp.get(fips),
                "median_income": income.get(fips, {}).get("median_income"),
                "mean_income": income.get(fips, {}).get("mean_income"),
            }
            rucc_data = rucc.get(fips, {})
            economics["rural_urban_code"] = rucc_data.get("code")
            economics["rural_urban_label"] = rucc_data.get("label")
            economics["is_rural"] = rucc_data.get("is_rural", False)
            economics["is_metro"] = rucc_data.get("is_metro", False)
            all_economics[fips] = economics

    _log.info("County economics built: %d counties across %s", len(all_economics), states)
    return all_economics
