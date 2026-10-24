# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Audit engine (EPIC-010-F-007) -- the deterministic cross-section sampler and
the recomputation that affirms a published record against its source.

The ProviderMedicare collection holds 26.7 million records (measured
2026-10-23), too many to re-read in full. The audit instead draws a
cross-section and proves those records from the rows that built them, which is
a stronger claim than re-validating the collection against its own schema: a
schema sweep only shows the builder agrees with its own output, while a
recomputation shows the output agrees with CMS.

THE SAMPLE IS DETERMINISTIC -- no random number generator, no stored seed. The
same data yields the same sample on any machine, so an auditor can reproduce
the selection and check the same records. Selection runs in two parts:

  1. CLASSIFY every candidate record on three axes, all computed from this
     year's own data so the sample moves with the data rather than with a
     hardcoded list:
       * clinical -- the ICD-10 chapter of the molecule's indications (the
         leading letter of its lowest-sorting level-3 code), or a class of its
         own when the molecule carries no indication. Measured: 1,042 distinct
         level-3 codes across 25 chapters, and 3,752 of 20,142 molecules carry
         no indication at all, so chapters are the axis that fits a sample and
         "no indication" is a real stratum.
       * volume -- the molecule's prescriber count ranked into bands, so a
         statin and an orphan oncology drug both appear.
       * fold depth -- how many source rows built the record: 1, 2-5, or 6+.
         Measured: 95.66% of records come from one row and 4.34% from 2 to 21,
         so a uniform draw would spend almost its whole budget on the path
         where the fold does nothing.
  2. PICK one record per non-empty cell, ordered by sha256(npi + molecule_id)
     and taking the lowest -- deterministic, and a hash rather than
     alphabetical order so the choice is not biased toward molecules whose
     names begin with A.

When the cap is smaller than the number of non-empty cells, cells fill in a
fixed priority order -- deepest fold first, then the rarest volume band, then
chapter spread -- so the thin, risky cells are never the ones dropped.
"""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from chathealthy_lib.logging_service import ChatHealthyLoggingService

_log = ChatHealthyLoggingService()

NO_INDICATION = "none"


def icd10_chapter(indications: Iterable[dict] | None) -> str:
    """The clinical class of a molecule: the leading letter of its
    lowest-sorting ICD-10 level-3 code, or 'none' when it carries no
    indication. One class per molecule, so a molecule lands in one cell."""
    codes: list[str] = []
    for ind in indications or []:
        icd = (ind or {}).get("icd10") or {}
        codes.extend(str(c) for c in (icd.get("level3") or []) if c)
    if not codes:
        return NO_INDICATION
    return sorted(codes)[0][:1].upper()


def fold_depth_band(record_count: int | None) -> str:
    """The contributing-row depth band: the fold does nothing at 1, does
    arithmetic at 2-5, and is at its most exercised at 6+."""
    n = int(record_count or 0)
    if n <= 1:
        return "1"
    if n <= 5:
        return "2-5"
    return "6+"


def volume_bands(prescriber_counts: dict[str, int], bands: int) -> dict[str, int]:
    """Rank molecules by prescriber count into equal-population bands, band 0
    being the highest volume. Computed from this year's data, so what counts
    as a high-volume molecule or an orphan is the year's own answer."""
    if bands < 1:
        bands = 1
    ordered = sorted(prescriber_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    out: dict[str, int] = {}
    size = max(1, -(-len(ordered) // bands))
    for position, (mid, _count) in enumerate(ordered):
        out[mid] = min(bands - 1, position // size)
    return out


def selection_key(npi: str, mid: str) -> str:
    """The deterministic order within a cell."""
    return hashlib.sha256(f"{npi}|{mid}".encode("utf-8")).hexdigest()


def cell_priority(cell: tuple[str, int, str], bands: int) -> tuple:
    """Fill order when the cap bites: deepest fold first, then the rarest
    volume band, then chapter spread."""
    chapter, band, depth = cell
    depth_rank = {"6+": 0, "2-5": 1, "1": 2}.get(depth, 3)
    band_rank = (bands - 1) - band
    return (depth_rank, band_rank, chapter)


def choose_sample(candidates: list[dict], prescriber_counts: dict[str, int],
                  bands: int, cap: int) -> list[dict]:
    """The cross-section: one record per non-empty cell, deterministic.

    candidates carry npi, molecule_id, record_count and the molecule's
    indications. Returns the chosen records, each stamped with the cell it
    represents so the report can state its coverage.
    """
    band_of = volume_bands(prescriber_counts, bands)
    by_cell: dict[tuple[str, int, str], list[dict]] = {}
    for rec in candidates:
        mid = str(rec.get("molecule_id") or "")
        cell = (icd10_chapter(rec.get("indications")),
                band_of.get(mid, 0),
                fold_depth_band(rec.get("record_count")))
        by_cell.setdefault(cell, []).append(rec)

    chosen: list[dict] = []
    for cell in sorted(by_cell, key=lambda c: cell_priority(c, bands)):
        members = by_cell[cell]
        pick = min(members, key=lambda r: selection_key(
            str(r.get("npi") or ""), str(r.get("molecule_id") or "")))
        pick = dict(pick)
        pick["_audit_cell"] = {"icd10_chapter": cell[0], "volume_band": cell[1],
                               "fold_depth": cell[2]}
        chosen.append(pick)
        if len(chosen) >= cap:
            break
    return chosen


def recompute_from_source(rows: list[dict]) -> dict[str, Any]:
    """The auditor's own fold, computed from the source rows, independent of
    whatever the builder wrote."""
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_providermedicare_engine import (  # noqa: PLC0415
        fold_rows)
    return fold_rows(rows)


def compare(published: dict, recomputed: dict) -> list[str]:
    """What disagrees between a published record and the recomputation.
    Returns one plain sentence per disagreement; empty means they agree."""
    problems: list[str] = []
    for field in ("tot_clms", "tot_day_suply", "record_count"):
        want, got = recomputed.get(field), published.get(field)
        if want != got:
            problems.append(
                f"{field}: source rows give {want!r}, record carries {got!r}")
    for field in ("brand_names", "generic_names"):
        want = sorted(recomputed.get(field) or [])
        got = sorted(published.get(field) or [])
        if want != got:
            problems.append(
                f"{field}: source rows give {want}, record carries {got}")
    return problems
