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

MANY ROWS, ONE WRITER, ONE WRITE (operator ruling 2026-10-24). A record's rows
all carry one CMS state, so the worker holding that state holds every row of
that record: it folds them in memory and writes the finished record once. No
merge, no markers, no replay reconciliation -- this is an ETL process, not a
banking transaction. A partition that dies fails the run, and the run is
re-driven from the clear, against an empty collection.

The record carries source_state only so the later steps can fan out on it;
drop_transient_attributes_and_records drops it. The geography that matters is
the provider's practice address, never the CMS prescriber state.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from chathealthy_lib.logging_service import ChatHealthyLoggingService

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


def _staging_pages(partd_coll, state: str, page_size: int = 5000):
    """Yield the state's staged rows page by page, holding no cursor."""
    last_id = None
    while True:
        query: dict[str, Any] = {f"raw.{PARTD_COLS['state']}": state}
        if last_id is not None:
            query["_id"] = {"$gt": last_id}
        page = list(partd_coll.find(query, sort=[("_id", 1)], limit=page_size))
        if not page:
            return
        last_id = page[-1]["_id"]
        yield from page


def record_id(npi: str, mid: str) -> dict:
    """The record key: NPI and molecule (REQ-B-001)."""
    return {"npi": str(npi), "molecule_id": str(mid)}


def fold_rows(rows: list[dict]) -> dict:
    """Fold every source row of one (NPI, molecule) into that record's drug
    facts (REQ-B-006).

    All of the record's rows are here, because they all carry one CMS state
    and one worker holds that state. Brand and generic names accumulate as
    first-seen-ordered arrays with blanks dropped; the two quantities sum over
    the rows, coerced from the source's strings.
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


def record_document(npi: str, mid: str, facts: dict, source_state: str) -> dict:
    """The finished record, written once by the one worker that holds its rows.

    source_state is the record's fan-out key for the steps after the base;
    drop_transient_attributes_and_records removes it at the end.
    """
    return {
        "_id": record_id(npi, mid),
        "npi": str(npi),
        "molecule_id": str(mid),
        "source_state": str(source_state),
        "medicare_population": "Part D including Medicare Advantage",
        **facts,
    }


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
    inserts one finished record per group. A row whose generic name mints no
    molecule, or that carries no NPI, is counted and skipped: it cannot key a
    record, and the no-molecule bucket is reported for the audit's arithmetic.

    The staging read is paged by _id rather than held on one cursor: the fold
    is fast but 61 workers reading 28M rows is not, and a cursor idle past the
    server's reaper comes back CursorNotFound.
    """
    rows_by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    no_molecule_rows = 0
    no_npi_rows = 0
    total_rows = 0

    for doc in _staging_pages(partd_coll, state):
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
    docs: list[dict] = []
    applied = 0
    for (npi, mid), group in rows_by_key.items():
        docs.append(record_document(npi, mid, fold_rows(group), state))
        if len(docs) >= batch_size:
            target_coll.insert_many(docs, ordered=False)
            applied += len(docs)
            docs = []
    if docs:
        target_coll.insert_many(docs, ordered=False)
        applied += len(docs)

    result = {
        "partition": unit,
        "rows_read": total_rows,
        "records_written": applied,
        "no_molecule_rows": no_molecule_rows,
        "no_npi_rows": no_npi_rows,
    }
    _log.LogPipeline(
        "INFO",
        "CMS_NPI_PrescriberBase[%s]: rows=%d records=%d no_molecule=%d no_npi=%d",
        unit, total_rows, applied, no_molecule_rows, no_npi_rows)
    return result
