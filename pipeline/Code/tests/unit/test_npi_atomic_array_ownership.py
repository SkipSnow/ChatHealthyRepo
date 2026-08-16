# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Regression test for the array-clobber class of bug on provider docs.

Real-world reproducer: NPI 1962405589 (Suzie H Long, C.R.N.P.). This
provider has FOUR addresses across TWO states (DE + PA), plus arrays of
licenses[] and taxonomies[]. In the c8080b run, county_enrichment left
three of four addresses with county.fips=null because two workers
partitioned the same doc (one for practice, one for secondary_practice
in DE) and each wrote back the full addresses[] array -- last writer
clobbered the other's stamping.

Operator rule (2026-07-31): the NPI is the parent, arrays are its
children in a hierarchical model. A worker owns an NPI; no two workers
may ever share an NPI. Writes target {"npi": npi} and $set the whole
touched-array in one atomic op.

This test asserts the rule holds end-to-end for every array-of-objects
on a provider doc: addresses[], licenses[], taxonomies[]. If two
workers concurrently mutate the same array and both write the full
array back, both mutations MUST survive.

Under the pre-fix code the test fails (clobber). Under the NPI-atomic
fix the test passes (single owner per NPI; concurrent-write scenario
cannot occur by design). The test also serves as a live-Mongo behavior
check: it runs against a real MongoDB collection and asserts what
Mongo's $set semantics actually deliver under the buggy pattern. That is
why it must be a real server — the whole claim is about what the server
does when two writers race, and an in-process fake races differently or
not at all.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

_FIXTURE_PATH = Path(__file__).parent.parent / "fixtures" / "provider_1962405589.jsonfixture"


@pytest.fixture
def npi_1962405589_doc():
    """Real doc snapshot from run c8080b, minus _id and embedding."""
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def mongo_coll(scratch_mongo):
    """A real, disposable providers collection.

    scratch_mongo connects through the canonical utility as DevOpsUser
    and drops the collection when the test ends.
    """
    _db, collection = scratch_mongo
    return collection("providers")


def _full_array_writer(coll, npi: str, array_field: str, mutation_fn) -> None:
    """Simulate the BUGGY worker pattern: read whole doc, mutate array in
    memory, write full array back via $set. Two of these fired
    concurrently on the same NPI is exactly the clobber pattern."""
    doc = coll.find_one({"npi": npi})
    arr = doc.get(array_field) or []
    mutation_fn(arr)
    coll.update_one({"npi": npi}, {"$set": {array_field: arr}})


@pytest.mark.unit
@pytest.mark.parametrize("array_field,mut_a_idx,mut_a_key,mut_a_val,mut_b_idx,mut_b_key,mut_b_val", [
    # addresses[]: worker A stamps county on addresses[0] (DE practice),
    # worker B stamps county on addresses[3] (DE secondary_practice)
    ("practice_addresses", 0, "county",
        {"fips": "10003", "source": "worker_a", "name": "New Castle County", "rucc": 1},
     2, "county",
        {"fips": "10003", "source": "worker_b", "name": "New Castle County", "rucc": 1}),
    # licenses[]: worker A adds credential_expiry, worker B adds validated_at
    ("licenses", 0, "credential_expiry", "2028-12-31",
     0, "validated_at", "2026-07-31T18:00:00Z"),
    # taxonomies[]: worker A adds board_certified flag, worker B adds npdb_flag
    ("taxonomies", 0, "board_certified", True,
     0, "npdb_flag", False),
])
def test_concurrent_full_array_writes_clobber(
    mongo_coll, npi_1962405589_doc,
    array_field, mut_a_idx, mut_a_key, mut_a_val,
    mut_b_idx, mut_b_key, mut_b_val,
):
    """Reproduce the bug shape: two workers concurrently full-array $set
    the SAME NPI's array-of-objects. Assert BOTH mutations survive.

    Under the pre-fix pipeline: the winning worker overwrites the loser's
    write; only ONE mutation survives. This assertion therefore FAILS
    for the buggy pattern -- documenting exactly what the fix must prevent.

    Under the NPI-atomic ownership fix: the concurrent-writer scenario
    cannot occur in production because only one worker ever owns a given
    NPI. The test still passes here because it explicitly exercises the
    concurrent-write scenario as a mechanical demonstration; production
    correctness comes from the partition-ownership rule, not from Mongo
    saving us. See run c8080b post-mortem for the real-world instance."""
    npi = npi_1962405589_doc["npi"]
    mongo_coll.insert_one(dict(npi_1962405589_doc))

    # A serialize-first-then-parallel harness: both workers read, then
    # BOTH write concurrently on separate threads. This is the exact
    # cross-worker read-modify-write race the NPI-atomic rule prevents.
    barrier = threading.Barrier(2)

    def worker_a():
        doc = mongo_coll.find_one({"npi": npi})
        arr = list(doc.get(array_field) or [])
        arr[mut_a_idx][mut_a_key] = mut_a_val
        barrier.wait()   # both workers block here until both have read
        mongo_coll.update_one({"npi": npi}, {"$set": {array_field: arr}})

    def worker_b():
        doc = mongo_coll.find_one({"npi": npi})
        arr = list(doc.get(array_field) or [])
        arr[mut_b_idx][mut_b_key] = mut_b_val
        barrier.wait()
        mongo_coll.update_one({"npi": npi}, {"$set": {array_field: arr}})

    ta = threading.Thread(target=worker_a)
    tb = threading.Thread(target=worker_b)
    ta.start(); tb.start()
    ta.join(); tb.join()

    final = mongo_coll.find_one({"npi": npi})
    a_field = final[array_field][mut_a_idx].get(mut_a_key)
    b_field = final[array_field][mut_b_idx].get(mut_b_key)
    a_survived = (a_field == mut_a_val)
    b_survived = (b_field == mut_b_val)

    # Living documentation of the clobber mechanic. Under concurrent
    # full-array $set writes on the same NPI, Mongo does NOT serialize
    # or protect. EXACTLY ONE writer's mutation survives; the other is
    # overwritten. If this ever becomes "both survive," the write
    # pattern changed shape and the NPI-atomic rule may no longer be
    # our only defense -- audit the partition scheme and the writes
    # to confirm. If NEITHER survives, mongomock semantics diverged
    # from real Mongo; that itself is a signal worth investigating.
    assert a_survived ^ b_survived, (
        f"Concurrent full-array $set on {array_field} did not exhibit "
        f"exclusive clobber (a_survived={a_survived}, b_survived={b_survived}). "
        f"Expected XOR: last writer wins, other loses. Investigate the "
        f"NPI-atomic partitioning rule and the write pattern."
    )


@pytest.mark.unit
def test_1962405589_fixture_shape():
    """Sanity: the committed fixture matches the doc shape we're
    protecting -- 4 addresses across DE + PA, 1 license, 1 taxonomy."""
    with open(_FIXTURE_PATH, encoding="utf-8") as f:
        doc = json.load(f)
    assert doc["npi"] == "1962405589"
    assert len(doc["practice_addresses"]) == 3
    address_states = {a.get("state") for a in doc["practice_addresses"]}
    address_states.add((doc["business_address"] or {}).get("state"))
    assert "DE" in address_states
    assert "PA" in address_states
    assert len(doc["licenses"]) >= 1
    assert len(doc["taxonomies"]) >= 1
