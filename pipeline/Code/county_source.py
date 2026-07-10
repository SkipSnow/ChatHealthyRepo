# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Schema-compliant county source labels — ProviderRecordSchema county.source enum."""

from __future__ import annotations

ZIP_CROSSWALK = "zip_crosswalk"
CENSUS_BATCH = "census_batch"
NPPES_REGISTRY = "nppes_registry"
GOOGLE_MAPS = "google_maps"
COUNTY_UNRESOLVABLE = "county_unresolvable"

SCHEMA_SUCCESS_SOURCES = frozenset({
    ZIP_CROSSWALK,
    CENSUS_BATCH,
    NPPES_REGISTRY,
    GOOGLE_MAPS,
})

CASCADE_ORDER = (ZIP_CROSSWALK, CENSUS_BATCH, NPPES_REGISTRY, GOOGLE_MAPS)

LEGACY_SOURCE_MAP = {
    "crosswalk_pass1": ZIP_CROSSWALK,
    "geocoder_pass2_batch": CENSUS_BATCH,
    "geocoder_pass6_nppes": NPPES_REGISTRY,
    "geocoder_pass4_maps": GOOGLE_MAPS,
    "geocoder_pass2_failed": COUNTY_UNRESOLVABLE,
    "geocoder_pass4_failed": COUNTY_UNRESOLVABLE,
    "geocoder_pass6_failed": COUNTY_UNRESOLVABLE,
    "geocoder_failed": COUNTY_UNRESOLVABLE,
}


def normalize_county_source(source: str | None) -> str | None:
    if source is None:
        return None
    return LEGACY_SOURCE_MAP.get(source, source)


def terminal_unresolvable_county() -> dict:
    return {"fips": None, "name": "", "source": COUNTY_UNRESOLVABLE}


def normalize_address_county(addr: dict) -> None:
    county = addr.get("county")
    if not isinstance(county, dict):
        return
    county.pop("zip_ratio", None)
    src = normalize_county_source(county.get("source"))
    if src:
        county["source"] = src
    addr["county"] = county
