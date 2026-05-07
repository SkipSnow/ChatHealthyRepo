"""County enrichment SLA test.

Asserts EPIC-010-F-007-S-011-REQ-B-001:
  "97% or more of the total Provider records MUST have county data
   associated with them."

A provider record "has county data" when at least one of:
  - doc-level county.fips is non-null (legacy / single-address shape)
  - any practice_address[i].county.fips is non-null (multi-practice-address shape)
  - mailing_address.county.fips is non-null (billing-derived enrichment)

The test reads MONGO_connectionString from the environment, defaults the
collection to dev_PublicHealthData.providers, and is overridable by the
PROVIDER_COLLECTION env var so it can target test_providers,
test_multiAddress_provider, providers_enriched, or any future per-env name
without code change.

Skipped if MONGO_connectionString is not set (e.g. CI without secrets).
"""

import os

import pytest


SLA_THRESHOLD = 0.97
DEFAULT_COLLECTION = "dev_PublicHealthData.providers"


def _connect():
    """Return the providers collection. Raises pytest.skip if no Mongo URI."""
    conn = os.environ.get("MONGO_connectionString") or os.environ.get("MONGO_URI")
    if not conn:
        pytest.skip("MONGO_connectionString not set; skipping live SLA check")
    from pymongo import MongoClient

    coll_path = os.environ.get("PROVIDER_COLLECTION", DEFAULT_COLLECTION)
    db_name, coll_name = coll_path.split(".", 1)
    client = MongoClient(conn, serverSelectionTimeoutMS=60_000, socketTimeoutMS=120_000)
    return client[db_name][coll_name], coll_path


def test_county_enrichment_sla_above_97_percent():
    coll, coll_path = _connect()

    total = coll.estimated_document_count()
    if total == 0:
        pytest.skip(f"{coll_path} is empty; nothing to assert SLA against")

    with_county_query = {
        "$or": [
            {"county.fips": {"$ne": None}},
            {"practice_address.county.fips": {"$ne": None}},
            {"mailing_address.county.fips": {"$ne": None}},
        ]
    }
    with_county = coll.count_documents(with_county_query)
    ratio = with_county / total

    print(
        f"\nCounty SLA against {coll_path}: "
        f"{with_county:,} / {total:,} = {ratio:.4%} "
        f"(threshold {SLA_THRESHOLD:.0%})"
    )

    assert ratio >= SLA_THRESHOLD, (
        f"County enrichment SLA breach on {coll_path}: "
        f"{ratio:.4%} of records have county data, below the {SLA_THRESHOLD:.0%} "
        f"threshold required by EPIC-010-F-007-S-011-REQ-B-001."
    )
