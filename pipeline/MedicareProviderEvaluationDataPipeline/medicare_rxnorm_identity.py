# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""RxNorm identity for a molecule: the deterministic CMS-to-FDA join key.

A CMS Part D row names a drug two ways and neither is an identifier:
Brnd_Name and Gnrc_Name. Gnrc_Name is CMS's own house style -- measured
2026-10-26, `Rsv Vacc, Pref A And Pref B/Pf` appears in 0 of 1,213,242
RXNCONSO rows across all 13 source vocabularies -- while the Brnd_Name on
that same row, `Abrysvo`, is RXCUI 2642144 with TTY BN in both RXNORM and
MMSL. So the brand is the identifier and the generic is a label.

The ladder, in order:

  1 Brnd_Name  -> an RXNCONSO row with TTY BN  -> follow RXNREL to that
    brand's ingredient (TTY IN)
  2 Gnrc_Name  -> an RXNCONSO row with TTY IN directly
  3 neither    -> no identity, and the caller records a finding

UNII comes from the same archive: the MTHSPL rows with TTY SU carry it in
their CODE column -- 83HN0GTJ6D for cyclosporine, KG60484QX9 for omeprazole.

Everything here is a pure function over RRF rows. The step feeds it the staged
RXNCONSO and RXNREL rows; nothing in this module touches Mongo or the network.

RXNCONSO columns:
  RXCUI|LAT|TS|LUI|STT|SUI|ISPREF|AUI|SAUI|SCUI|SDUI|SAB|TTY|CODE|STR|...
RXNREL columns:
  RXCUI1|RXAUI1|STYPE1|REL|RXCUI2|RXAUI2|STYPE2|RELA|RUI|SRUI|SAB|...
"""

from __future__ import annotations

from typing import Any, Iterable

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    molecule_canonical, normalize_brand)

# The two relationships that reach an ingredient from a brand, measured on the
# 2026-10-05 release: Restasis and Prilosec arrive by tradename_of; Abrysvo,
# whose brand is a two-antigen combination, arrives by has_precise_ingredient.
RELA_TO_INGREDIENT = ("tradename_of", "has_precise_ingredient",
                      "has_ingredient", "ingredient_of", "precise_ingredient_of",
                      "tradename_of_inverse")

INGREDIENT_TTYS = ("IN",)
BRAND_TTYS = ("BN",)
SUBSTANCE_SAB = "MTHSPL"
SUBSTANCE_TTY = "SU"

# The resolution path recorded on each molecule, so a reader can tell which
# rung of the ladder answered.
PATH_BRAND = "brand"
PATH_GENERIC = "generic"


class RxNormIndex:
    """The three lookups the ladder needs, built once per run."""

    def __init__(self) -> None:
        self.ingredient_by_name: dict[str, str] = {}
        self.brand_by_name: dict[str, str] = {}
        self.ingredients_of: dict[str, list[str]] = {}
        self.unii_by_rxcui: dict[str, str] = {}
        self.ingredient_name: dict[str, str] = {}
        self.conso_rows = 0
        self.rel_rows = 0

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return (f"RxNormIndex(ingredients={len(self.ingredient_by_name)}, "
                f"brands={len(self.brand_by_name)}, "
                f"unii={len(self.unii_by_rxcui)})")


def _fields(row: Any) -> list[str]:
    """One RRF row as its columns, whether it arrives as a staged document or
    a raw pipe-delimited line."""
    if isinstance(row, dict):
        raw = row.get("raw") or row
        return raw if isinstance(raw, list) else []
    if isinstance(row, (bytes, bytearray)):
        row = row.decode("utf-8", "replace")
    if isinstance(row, str):
        return row.rstrip("\n").split("|")
    return []


def _conso(row: Any) -> dict | None:
    """One RXNCONSO row as the four values the ladder reads."""
    if isinstance(row, dict):
        raw = row.get("raw") or row
        if isinstance(raw, dict):
            return {"rxcui": str(raw.get("RXCUI") or "").strip(),
                    "sab": str(raw.get("SAB") or "").strip(),
                    "tty": str(raw.get("TTY") or "").strip(),
                    "code": str(raw.get("CODE") or "").strip(),
                    "str": str(raw.get("STR") or "")}
    parts = _fields(row)
    if len(parts) < 15:
        return None
    return {"rxcui": parts[0].strip(), "sab": parts[11].strip(),
            "tty": parts[12].strip(), "code": parts[13].strip(),
            "str": parts[14]}


def _rel(row: Any) -> dict | None:
    """One RXNREL row as the three values the walk reads."""
    if isinstance(row, dict):
        raw = row.get("raw") or row
        if isinstance(raw, dict):
            return {"rxcui1": str(raw.get("RXCUI1") or "").strip(),
                    "rxcui2": str(raw.get("RXCUI2") or "").strip(),
                    "rela": str(raw.get("RELA") or "").strip()}
    parts = _fields(row)
    if len(parts) < 11:
        return None
    return {"rxcui1": parts[0].strip(), "rxcui2": parts[4].strip(),
            "rela": parts[7].strip()}


def build_index(conso_rows: Iterable[Any],
                rel_rows: Iterable[Any]) -> RxNormIndex:
    """The run's RxNorm index from the staged RXNCONSO and RXNREL rows.

    Brand names are folded with normalize_brand -- case, whitespace and
    hyphen-and only -- because a brand is matched as written. Ingredient names
    are folded with molecule_canonical, which also strips salt and dose-form
    words, so CMS's `ATORVASTATIN CALCIUM` reaches RxNorm's `atorvastatin`.
    First row wins for each folded name, so the index is stable under row
    order.
    """
    index = RxNormIndex()

    for row in conso_rows:
        rec = _conso(row)
        if rec is None or not rec["rxcui"]:
            continue
        index.conso_rows += 1
        tty, text = rec["tty"], rec["str"]
        if tty in INGREDIENT_TTYS:
            index.ingredient_name.setdefault(rec["rxcui"], text)
            key = molecule_canonical(text)
            if key:
                index.ingredient_by_name.setdefault(key, rec["rxcui"])
        elif tty in BRAND_TTYS:
            key = normalize_brand(text)
            if key:
                index.brand_by_name.setdefault(key, rec["rxcui"])
        if rec["sab"] == SUBSTANCE_SAB and tty == SUBSTANCE_TTY and rec["code"]:
            index.unii_by_rxcui.setdefault(rec["rxcui"], rec["code"])

    brands = set(index.brand_by_name.values())
    for row in rel_rows:
        rec = _rel(row)
        if rec is None or rec["rela"] not in RELA_TO_INGREDIENT:
            continue
        index.rel_rows += 1
        # RXNREL states a relationship in one direction only, and which end
        # carries the brand depends on the RELA. Both ends are considered and
        # only an end that is a known ingredient is kept.
        for near, far in ((rec["rxcui2"], rec["rxcui1"]),
                          (rec["rxcui1"], rec["rxcui2"])):
            if near in brands and far in index.ingredient_name:
                reached = index.ingredients_of.setdefault(near, [])
                if far not in reached:
                    reached.append(far)
    return index


def resolve_identity(index: RxNormIndex, *, brand_name: Any,
                     generic_name: Any) -> dict:
    """One molecule's identity by the ladder.

    Returns {"rxcui_in", "unii", "path"} with every value None and path None
    when neither rung answers. A brand that reaches more than one ingredient
    -- a combination product -- carries the first in RXNREL order and the rest
    under "also", so nothing is silently dropped.
    """
    brand_key = normalize_brand(brand_name) if brand_name else ""
    if brand_key:
        brand_rxcui = index.brand_by_name.get(brand_key)
        reached = index.ingredients_of.get(brand_rxcui or "", [])
        if reached:
            primary = reached[0]
            return {
                "rxcui_in": primary,
                "unii": index.unii_by_rxcui.get(primary),
                "path": PATH_BRAND,
                "also": [{"rxcui_in": r,
                          "unii": index.unii_by_rxcui.get(r)}
                         for r in reached[1:]],
            }

    generic_key = molecule_canonical(generic_name) if generic_name else ""
    if generic_key:
        rxcui = index.ingredient_by_name.get(generic_key)
        if rxcui:
            return {"rxcui_in": rxcui,
                    "unii": index.unii_by_rxcui.get(rxcui),
                    "path": PATH_GENERIC,
                    "also": []}

    return {"rxcui_in": None, "unii": None, "path": None, "also": []}
