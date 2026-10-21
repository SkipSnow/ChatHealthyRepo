# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The rrf_rxnconso staging format (EPIC-010-F-007, LLD v83 III.9): RXNCONSO.RRF
is headerless pipe-delimited with one trailing delimiter per line; the iterator
names each cell by the fixed 18-column RXNCONSO layout."""

from pipeline.provider_base.staging_loader import (
    _RXNCONSO_COLUMNS, _iter_rrf_rxnconso_rows)


def _line(**cells) -> str:
    """One RXNCONSO line: 18 positional cells by column name, with the real
    file's trailing delimiter."""
    vals = [cells.get(col, "") for col in _RXNCONSO_COLUMNS]
    return "|".join(vals) + "|"


def _write(tmp_path, text):
    p = tmp_path / "RXNCONSO.RRF"
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_rows_parse_by_fixed_column_layout(tmp_path):
    text = _line(RXCUI="83367", LAT="ENG", SAB="RXNORM", TTY="IN",
                 CODE="83367", STR="atorvastatin", SUPPRESS="N") + "\n"
    rows = list(_iter_rrf_rxnconso_rows(_write(tmp_path, text)))
    assert len(rows) == 1
    row = rows[0]
    assert row["RXCUI"] == "83367"
    assert row["TTY"] == "IN"
    assert row["STR"] == "atorvastatin"
    assert row["SAB"] == "RXNORM"
    assert row["SUPPRESS"] == "N"


def test_trailing_delimiter_and_blank_lines_handled(tmp_path):
    text = (_line(RXCUI="1", LAT="ENG", SAB="RXNORM", TTY="IN", STR="alpha",
                  SUPPRESS="N") + "\n"
            + "\n"
            + _line(RXCUI="2", LAT="ENG", SAB="RXNORM", TTY="PIN", STR="beta",
                    SUPPRESS="N") + "\r\n")
    rows = list(_iter_rrf_rxnconso_rows(_write(tmp_path, text)))
    assert [r["RXCUI"] for r in rows] == ["1", "2"]
    assert [r["STR"] for r in rows] == ["alpha", "beta"]
    assert [r["TTY"] for r in rows] == ["IN", "PIN"]
    # exactly one trailing empty cell is dropped: every named column present,
    # no overflow _colN cells
    for r in rows:
        for col in _RXNCONSO_COLUMNS:
            assert col in r
        assert not [k for k in r if k.startswith("_col")]


def test_layout_is_the_18_column_rxnconso_contract():
    assert _RXNCONSO_COLUMNS == (
        "RXCUI", "LAT", "TS", "LUI", "STT", "SUI", "ISPREF", "RXAUI", "SAUI",
        "SCUI", "SDUI", "SAB", "TTY", "CODE", "STR", "SRL", "SUPPRESS", "CVF")
