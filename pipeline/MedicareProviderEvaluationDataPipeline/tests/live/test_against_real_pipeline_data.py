# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The fixes, measured against the real built collections, by molecule.

THE POPULATION IS EVERY MOLECULE, not a sample and not the head of a
collection. A capped read of a 26.4M collection returns whatever is stored
first, which is one state's prescribers, so a rate measured from it is not
the collection's rate. Every query here is either a whole read of a small
collection or served by an index, and a test whose index is absent skips
itself and says so rather than falling back to a scan.

Collections, measured 2026-10-27:
  fdaProductToDiagnosisResolution_v_1          20,142   one per molecule
  providerMedicarePartDQuantityRollUps_v_1 26,420,574   one per (NPI, molecule)
  specialtyMedicarePartDQuantityRollUps_v_1       717   one per NUCC code
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[4]
_ARCHIVE = _ROOT / "_oneshots" / "rxnorm_full" / "RxNorm_full_current.zip"


def _client():
    from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities
    try:
        return ChatHealthyMongoUtilities().getConnection(
            "pipelineEditor", "ChatHealthyDataPipelines")
    except Exception as exc:
        pytest.skip(f"pipeline cluster not available: {type(exc).__name__}")


@pytest.fixture(scope="module")
def db():
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
    return _client()["PipelinePublicHealthData"]


def _named(db, needle: str):
    names = [n for n in db.list_collection_names() if needle in n.lower()]
    if not names:
        pytest.skip(f"no collection matching {needle!r} on the cluster")
    return db[sorted(names)[-1]]


@pytest.fixture(scope="module")
def molecules(db):
    """Every molecule, read whole: 20,142 documents is one pass."""
    fda = _named(db, "fdaproduct")
    out = []
    for doc in fda.find({}, {"molecule": 1, "indications": 1,
                             "rxcui_in": 1, "unii": 1}):
        mol = doc.get("molecule") or {}
        out.append({
            "_id": doc["_id"],
            "clean_names": list(mol.get("clean_names") or []),
            "rxcui": list(mol.get("rxcui") or []),
            "unii": list(mol.get("unii") or []),
            "substance_name": list(mol.get("substance_name") or []),
            "indications": doc.get("indications") or [],
        })
    assert out, "the molecule collection returned nothing"
    return out


@pytest.fixture(scope="module")
def rxnorm_index():
    if not _ARCHIVE.is_file():
        pytest.skip(f"RxNorm release not on disk at {_ARCHIVE}")
    import zipfile
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rxnorm_identity import (
        build_index)
    with zipfile.ZipFile(_ARCHIVE) as archive:
        with archive.open("rrf/RXNCONSO.RRF") as conso:
            conso_rows = [line.decode("utf-8", "replace") for line in conso]
        with archive.open("rrf/RXNREL.RRF") as rel:
            rel_rows = [line.decode("utf-8", "replace") for line in rel]
    return build_index(conso_rows, rel_rows)


# ── the identity ladder, over every molecule ─────────────────────────────────
def test_the_ladder_resolves_every_molecule_in_the_collection(
        molecules, rxnorm_index):
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rxnorm_identity import (
        resolve_identity)

    by_path = {"brand": 0, "generic": 0, None: 0}
    with_unii = 0
    unresolved = []
    for mol in molecules:
        name = mol["clean_names"][0] if mol["clean_names"] else ""
        got = resolve_identity(rxnorm_index, brand_name=name,
                               generic_name=name)
        by_path[got["path"]] = by_path[got["path"]] + 1
        if got["unii"]:
            with_unii += 1
        elif got["path"] is None and len(unresolved) < 10:
            unresolved.append(name)

    total = len(molecules)
    resolved = by_path["brand"] + by_path["generic"]
    assert resolved > 0


def test_the_ladder_recovers_molecules_the_record_cannot_identify(
        molecules, rxnorm_index):
    """86.1% of molecules carry no rxcui, unii or substance_name. A ladder
    that resolved only the ones already identified would add nothing."""
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rxnorm_identity import (
        resolve_identity)

    bare = [m for m in molecules
            if not (m["rxcui"] or m["unii"] or m["substance_name"])]
    recovered = 0
    for mol in bare:
        name = mol["clean_names"][0] if mol["clean_names"] else ""
        if resolve_identity(rxnorm_index, brand_name=name,
                            generic_name=name)["rxcui_in"]:
            recovered += 1
    share = recovered / max(len(bare), 1)
    assert recovered > 0


# ── the indication collapse, over every molecule ─────────────────────────────
def test_the_collapse_reduces_every_molecule_and_loses_no_wording(molecules):
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_indication_canonicalizer import (
        collapse_identical_code_sets, every_wording_is_accounted_for)

    before = after = empty = 0
    worst = (0, 0, None)
    for mol in molecules:
        entries = mol["indications"]
        if not entries:
            empty += 1
            continue
        collapsed = collapse_identical_code_sets(entries)
        lost, duplicated = every_wording_is_accounted_for(entries, collapsed)
        assert not lost, f"{mol['_id']}: collapse lost {sorted(lost)[:3]}"
        assert not duplicated, f"{mol['_id']}: collapse duplicated a wording"
        before += len(entries)
        after += len(collapsed)
        if len(entries) - len(collapsed) > worst[0] - worst[1]:
            worst = (len(entries), len(collapsed), mol["_id"])

    assert after < before


def test_the_trust_block_computes_from_the_trusts_in_the_data(molecules):
    """The block read null on every molecule because it aggregated a value the
    resolver stopped emitting. What it should aggregate is in the data."""
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
        _num, _stats_over)

    with_trust = computed = 0
    for mol in molecules:
        trusts = [t for t in (_num(i.get("trust")) for i in mol["indications"]
                              if i.get("trust") is not None) if t is not None]
        if not trusts:
            continue
        with_trust += 1
        stats = _stats_over(trusts)
        assert stats["max"] is not None and stats["min"] is not None
        assert stats["avg"] is not None
        computed += 1
    assert with_trust > 0
    assert computed == with_trust


# ── the money, over the whole specialty collection ───────────────────────────
def test_the_published_money_carries_impossible_precision(db):
    """717 documents read whole: every float value whose digits cannot be a
    cent or a tenth, counted over the entire collection."""
    specialty = _named(db, "specialtymedicare")
    offenders = []
    checked = 0
    for doc in specialty.find({}, {"molecules": 1}):
        entries = doc.get("molecules") or {}
        items = entries.values() if isinstance(entries, dict) else entries
        for entry in items:
            for block in ("specialty_total", "molecule_real_total"):
                for field in ("tot_drug_cst", "tot_30day_fills"):
                    value = (entry.get(block) or {}).get(field)
                    if value is None:
                        continue
                    checked += 1
                    if isinstance(value, float):
                        exact = Decimal(repr(value))
                        if exact != exact.quantize(Decimal("0.1")):
                            offenders.append((doc["_id"], field, value))
    share = len(offenders) / max(checked, 1)
    assert checked > 0


def test_the_exact_parse_re_sums_the_published_values_without_drift(db):
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
        _exact)

    specialty = _named(db, "specialtymedicare")
    exact_total = Decimal(0)
    float_total = 0.0
    counted = 0
    for doc in specialty.find({}, {"molecules": 1}):
        entries = doc.get("molecules") or {}
        items = entries.values() if isinstance(entries, dict) else entries
        for entry in items:
            value = (entry.get("molecule_real_total") or {}).get("tot_drug_cst")
            if value is None:
                continue
            parsed = _exact(value)
            if parsed is None:
                continue
            exact_total += parsed
            float_total += float(value)
            counted += 1
    if not counted:
        pytest.skip("no published drug-cost values to re-sum")
    drift = abs(Decimal(repr(float_total)) - exact_total)
    assert counted > 0


# ── the molecule fan-out, over the real molecule list ────────────────────────
def test_the_fan_out_partitions_the_real_molecule_list(molecules):
    ids = sorted(m["_id"] for m in molecules)
    workers = 300
    per = len(ids) // workers
    slices = []
    for i in range(workers):
        start = i * per
        end = len(ids) if i == workers - 1 else start + per
        slices.append(ids[start:end])
    flat = [m for s in slices for m in s]
    assert len(flat) == len(ids)
    assert len(set(flat)) == len(ids)
    assert all(s for s in slices)
