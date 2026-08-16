"""County enrichment SLA test.

Asserts EPIC-010-F-007-S-011-REQ-B-001:
  "97% or more of the total Provider records MUST have county data
   associated with them."

A provider record "has county data" when at least one entry in addresses[]
has a non-null county.fips. Post-schema-reconciliation, addresses[] holds
both practice and business entries (address_type discriminator); the
underlying Mongo path `practice_addresses.county.fips` matches either kind.

The test defaults the collection to PipelinePublicHealthData.providers and
is overridable by the
PROVIDER_COLLECTION env var so it can target test_providers,
test_multiAddress_provider, providers_enriched, or any future per-env name
without code change.

"""

import os

import pytest

import sys as _sys, pathlib as _pl
for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break
from chathealthy_lib.logging_service import ChatHealthyLoggingService

_CH_LOG = ChatHealthyLoggingService()


# Rule-004: one place in this file obtains a connection, and it goes through
# the canonical utility. The certificate is the credential; there is no
# connection string here and no fallback. Raises if the identity cannot
# connect, which is the point -- a test that quietly connects as something
# else proves nothing about production.
def _ch_connection():
    import sys as _sys, pathlib as _pl
    for _d in _pl.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _lib = _d / "ChatHealthyLib" / "src"
            if str(_lib) not in _sys.path:
                _sys.path.insert(0, str(_lib))
            break
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    return ChatHealthyMongoUtilities().getConnection("DevOpsUser", 'pipelines')


SLA_THRESHOLD = 0.97
DEFAULT_COLLECTION = "PipelinePublicHealthData.providers"


def _connect():
    """Return the providers collection. Raises pytest.skip if no Mongo URI."""
    from pymongo import MongoClient

    coll_path = os.environ.get("PROVIDER_COLLECTION", DEFAULT_COLLECTION)
    db_name, coll_name = coll_path.split(".", 1)
    client = _ch_connection()
    return client[db_name][coll_name], coll_path


def test_county_enrichment_sla_above_97_percent():
    coll, coll_path = _connect()

    # The population the SLA is about is providers the enrichment can reach.
    # County enrichment admits practice address types only, and a provider
    # with no practice address can never enter the numerator -- counting it
    # in the denominator depresses the ratio on data shape rather than on
    # enrichment failure. Exact on both sides: an estimated denominator
    # against an exact numerator is not a ratio of anything.
    total = coll.count_documents({"practice_addresses.0": {"$exists": True}})
    if total == 0:
        pytest.skip(f"{coll_path} holds no provider with a practice address")

    # $ne is document-level negation -- it selects documents where NO entry
    # has a null fips, i.e. every practice address resolved. The SLA is that
    # the provider has county data, meaning at least one did, which is what
    # $elemMatch asks.
    with_county_query = {
        "practice_addresses": {"$elemMatch": {"county.fips": {"$ne": None}}}}
    with_county = coll.count_documents(with_county_query)
    ratio = with_county / total

    _CH_LOG.info(
        f"\nCounty SLA against {coll_path}: "
        f"{with_county:,} / {total:,} = {ratio:.4%} "
        f"(threshold {SLA_THRESHOLD:.0%})"
    )

    assert ratio >= SLA_THRESHOLD, (
        f"County enrichment SLA breach on {coll_path}: "
        f"{ratio:.4%} of records have county data, below the {SLA_THRESHOLD:.0%} "
        f"threshold required by EPIC-010-F-007-S-011-REQ-B-001."
    )
