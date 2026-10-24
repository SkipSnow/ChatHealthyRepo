# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""CMS_NPI_PrescriberBase engine (EPIC-010-F-007-S-002) -- one record per
molecule per provider, built from the CMS Part D file and nothing else.

Derived from the data, measured 2026-10-23 against the staged rows:

  * 28,023,892 source rows fold into 26,700,438 (NPI, molecule) records.
  * 95.66% of records come from ONE source row; 4.34% (1,157,841) come from
    2 to 21 rows -- several brand names of one molecule for one prescriber.
    Levothyroxine Sodium shows the shape: one prescriber, seven brands
    (Euthyrox, Levothyroxine Sodium, Levoxyl, Synthroid, Tirosint,
    Tirosint-Sol, Unithroid), whose claims fold to one total.
  * NOT ONE NPI's rows carry more than one CMS state, so the row's own
    Prscrbr_State_Abrvtn is a sound fan-out key: no Provider-collection
    lookup, no stamping pre-pass, no catch-all partition.
  * Tot_Clms and Tot_Day_Suply are present on 100.0% of rows and
    Bene_Sprsn_Flag is set on none, so the two carried quantities are never
    suppressed and need no null-versus-zero machinery.
  * Every source value is a string ('2830'), so quantities are coerced before
    they are summed.

MANY ROWS, MANY UNITS OF WORK, ONE RECORD (operator ruling 2026-10-23). Which
unit of work holds a record's rows is a runtime choice that may change, so the
design never assumes a record has one writer. Every write is
contribution_update: one atomic act that MERGES (arrays $addToSet, quantities
$inc) so contributions from different units add up, GATED on the unit's own
marker so a re-driven or twice-claimed unit applies exactly once. Mongo's
single-document atomicity makes the gate and the merge indivisible, so neither
a second contributor nor a replay can clobber or double-count.

The record carries source_state only so the enrichment steps can fan out on
it; the final task drops it.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    PARTD_COLS,
    molecule_id,
    partd_child,
)

_log = ChatHealthyLoggingService()

# The quantities that sum across a record's source rows (REQ-B-006). Both are
# per-row totals in the CMS source and neither counts people, so neither can
# double-count under the fold.
_SUMMED = ("tot_clms", "tot_day_suply")


def record_id(npi: str, mid: str) -> dict:
    """The record key: NPI and molecule (REQ-B-001)."""
    return {"npi": str(npi), "molecule_id": str(mid)}


def fold_rows(rows: list[dict]) -> dict:
    """Fold one unit of work's rows for one (NPI, molecule) into that record's
    CONTRIBUTION (REQ-B-006).

    A contribution, not a finished record: the write merges it into whatever
    is already there. Brand and generic names accumulate as first-seen-ordered
    arrays with blanks dropped; the two quantities sum over this unit's rows,
    coerced from the source's strings.
    """
    brands: list[str] = []
    generics: list[str] = []
    sums: dict[str, int] = {k: 0 for k in _SUMMED}

    for raw in rows:
        child = partd_child(raw)
        brand = (child.get("brnd_name") or "").strip()
        if brand and brand not in brands:
            brands.append(brand)
        generic = (child.get("gnrc_name") or "").strip()
        if generic and generic not in generics:
            generics.append(generic)
        for key in _SUMMED:
            value = child.get(key)
            if value is not None:
                sums[key] += value

    return {
        "brand_names": brands,
        "generic_names": generics,
        "record_count": len(rows),
        **{k: sums[k] for k in _SUMMED},
    }


def contribution_update(npi: str, mid: str, unit: str, facts: dict,
                        source_state: str) -> UpdateOne:
    """The one atomic write that applies a unit of work's contribution:
    mergeable AND idempotent (operator ruling 2026-10-23).

    Mergeable -- arrays $addToSet, quantities $inc, so contributions from
    different units add up instead of one replacing another.

    Idempotent -- the filter requires this unit's marker to be ABSENT and the
    update adds it, so a unit re-driven after a worker death, or claimed
    twice, applies exactly once; a replay is a silent no-op.

    source_state is $setOnInsert: it is the record's fan-out key for the
    enrichment steps, established by whichever unit creates the record and
    never rewritten.
    """
    inc: dict[str, int] = {"record_count": facts["record_count"]}
    for key in _SUMMED:
        inc[key] = facts[key]
    return UpdateOne(
        {"_id": record_id(npi, mid), "contributed_by": {"$ne": unit}},
        {"$addToSet": {"contributed_by": unit,
                       "brand_names": {"$each": facts["brand_names"]},
                       "generic_names": {"$each": facts["generic_names"]}},
         "$inc": inc,
         "$setOnInsert": {"npi": str(npi), "molecule_id": str(mid),
                          "source_state": str(source_state),
                          "medicare_population":
                              "Part D including Medicare Advantage"}},
        upsert=True)


def build_prescriber_base(
    *,
    state: str,
    partd_coll,
    target_coll,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Build one CMS-state partition of the base collection.

    Reads the staged Part D rows whose own Prscrbr_State_Abrvtn is this state
    -- no other source is consulted -- groups them by (NPI, molecule), and
    applies one contribution per group. A row whose generic name mints no
    molecule, or that carries no NPI, is counted and skipped: it cannot key a
    record, and the no-molecule bucket is reported for the audit's arithmetic.
    """
    rows_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    no_molecule_rows = 0
    no_npi_rows = 0
    total_rows = 0

    cursor = partd_coll.find({f"raw.{PARTD_COLS['state']}": state})
    for doc in cursor:
        raw = doc.get("raw") or {}
        total_rows += 1
        npi = str(raw.get(PARTD_COLS["npi"]) or "").strip()
        if not npi:
            no_npi_rows += 1
            continue
        mid = molecule_id(raw.get(PARTD_COLS["generic"]))
        if not mid:
            no_molecule_rows += 1
            continue
        rows_by_key[(npi, mid)].append(raw)

    unit = str(state)
    ops: list[UpdateOne] = []
    applied = 0
    for (npi, mid), group in rows_by_key.items():
        ops.append(contribution_update(npi, mid, unit, fold_rows(group), state))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            applied += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        applied += len(ops)

    result = {
        "partition": unit,
        "rows_read": total_rows,
        "records_contributed": applied,
        "no_molecule_rows": no_molecule_rows,
        "no_npi_rows": no_npi_rows,
    }
    _log.LogPipeline(
        "INFO",
        "CMS_NPI_PrescriberBase[%s]: rows=%d records=%d no_molecule=%d no_npi=%d",
        unit, total_rows, applied, no_molecule_rows, no_npi_rows)
    return result
