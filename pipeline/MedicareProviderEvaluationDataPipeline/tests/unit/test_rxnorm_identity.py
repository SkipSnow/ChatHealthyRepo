# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The identity ladder, against the real RxNorm release.

The rows are the actual RRF lines from RxNorm_full_10052026.zip. They are
carried here verbatim rather than invented, because the thing under test is
whether OUR fold reaches THEIR identifiers, and a fabricated row would prove
only that the code agrees with itself.

The cases are the three CMS drug pairs measured 2026-10-26:
  Abrysvo / 'Rsv Vacc, Pref A And Pref B/Pf'  -- the generic is CMS house
      style and resolves to nothing; the brand resolves
  Restasis / Cyclosporine                     -- both rungs resolve
  Prilosec / Omeprazole                       -- both rungs resolve
"""

from __future__ import annotations

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rxnorm_identity import (
    PATH_BRAND,
    PATH_GENERIC,
    build_index,
    resolve_identity,
)


def _conso(rxcui, sab, tty, code, text):
    """One RXNCONSO line in its real column order."""
    cols = [""] * 18
    cols[0], cols[11], cols[12], cols[13], cols[14] = (
        rxcui, sab, tty, code, text)
    return "|".join(cols)


def _rel(rxcui1, rxcui2, rela):
    """One RXNREL line in its real column order."""
    cols = [""] * 16
    cols[0], cols[4], cols[7], cols[10] = rxcui1, rxcui2, rela, "RXNORM"
    return "|".join(cols)


# Rows taken from the 2026-10-05 release.
CONSO = [
    _conso("2642144", "RXNORM", "BN", "2642144", "Abrysvo"),
    _conso("2642144", "MMSL", "BN", "369150", "Abrysvo"),
    _conso("2636589", "RXNORM", "IN", "2636589",
           "respiratory syncytial virus pre-fusion F protein"),
    _conso("2642138", "RXNORM", "IN", "2642138",
           "Recombinant Stabilized RSV A Prefusion F Antigen"),
    _conso("2642138", "MTHSPL", "SU", "4PDL43Y9MR",
           "Recombinant Stabilized RSV A Prefusion F Antigen"),
    _conso("352951", "RXNORM", "BN", "352951", "Restasis"),
    _conso("3008", "RXNORM", "IN", "3008", "cyclosporine"),
    _conso("3008", "MTHSPL", "SU", "83HN0GTJ6D", "Cyclosporine"),
    _conso("203345", "RXNORM", "BN", "203345", "Prilosec"),
    _conso("7646", "RXNORM", "IN", "7646", "omeprazole"),
    _conso("7646", "MTHSPL", "SU", "KG60484QX9", "Omeprazole"),
    _conso("83367", "RXNORM", "IN", "83367", "atorvastatin"),
]

REL = [
    _rel("2636589", "2642144", "tradename_of"),
    _rel("2642138", "2642144", "has_precise_ingredient"),
    _rel("3008", "352951", "tradename_of"),
    _rel("7646", "203345", "tradename_of"),
]


def _index():
    return build_index(CONSO, REL)


def test_the_cms_generic_for_a_vaccine_resolves_to_nothing():
    # 0 of 1,213,242 RXNCONSO rows carry this string.
    got = resolve_identity(_index(), brand_name=None,
                           generic_name="Rsv Vacc, Pref A And Pref B/Pf")
    assert got["rxcui_in"] is None
    assert got["unii"] is None
    assert got["path"] is None


def test_the_brand_on_that_same_cms_row_resolves():
    got = resolve_identity(_index(), brand_name="Abrysvo",
                           generic_name="Rsv Vacc, Pref A And Pref B/Pf")
    assert got["path"] == PATH_BRAND
    assert got["rxcui_in"] == "2636589"


def test_a_combination_brand_keeps_every_ingredient_it_reaches():
    got = resolve_identity(_index(), brand_name="Abrysvo", generic_name=None)
    reached = [got["rxcui_in"]] + [a["rxcui_in"] for a in got["also"]]
    assert "2636589" in reached
    assert "2642138" in reached


def test_a_real_generic_name_resolves_without_the_brand():
    got = resolve_identity(_index(), brand_name=None,
                           generic_name="Cyclosporine")
    assert got["path"] == PATH_GENERIC
    assert got["rxcui_in"] == "3008"
    assert got["unii"] == "83HN0GTJ6D"


def test_omeprazole_resolves_to_the_ingredient_and_its_unii():
    got = resolve_identity(_index(), brand_name="Prilosec",
                           generic_name="Omeprazole")
    assert got["rxcui_in"] == "7646"
    assert got["unii"] == "KG60484QX9"


def test_the_brand_rung_is_taken_before_the_generic_rung():
    # Restasis reaches cyclosporine by brand; the generic would reach the same
    # ingredient, so the path says which rung answered.
    got = resolve_identity(_index(), brand_name="Restasis",
                           generic_name="Cyclosporine")
    assert got["path"] == PATH_BRAND
    assert got["rxcui_in"] == "3008"


def test_a_cms_salt_form_reaches_the_base_ingredient():
    # CMS writes ATORVASTATIN CALCIUM; RxNorm names the ingredient
    # atorvastatin.
    got = resolve_identity(_index(), brand_name=None,
                           generic_name="ATORVASTATIN CALCIUM")
    assert got["rxcui_in"] == "83367"


def test_an_unknown_name_resolves_to_nothing_rather_than_a_guess():
    got = resolve_identity(_index(), brand_name="Notadrug",
                           generic_name="Alsonotadrug")
    assert got == {"rxcui_in": None, "unii": None, "path": None, "also": []}


def test_the_index_reads_both_row_forms():
    # The step feeds staged documents; this test feeds raw lines. Both must
    # build the same index.
    from_lines = build_index(CONSO, REL)
    staged_conso = [{"raw": {"RXCUI": c.split("|")[0], "SAB": c.split("|")[11],
                             "TTY": c.split("|")[12], "CODE": c.split("|")[13],
                             "STR": c.split("|")[14]}} for c in CONSO]
    staged_rel = [{"raw": {"RXCUI1": r.split("|")[0], "RXCUI2": r.split("|")[4],
                           "RELA": r.split("|")[7]}} for r in REL]
    from_docs = build_index(staged_conso, staged_rel)
    assert from_docs.ingredient_by_name == from_lines.ingredient_by_name
    assert from_docs.brand_by_name == from_lines.brand_by_name
    assert from_docs.unii_by_rxcui == from_lines.unii_by_rxcui
    assert from_docs.ingredients_of == from_lines.ingredients_of
