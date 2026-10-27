# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical indications, on the real wordings.

The entries are the actual indication texts and code sets from the FDA
records the operator inspected: the phenylephrine molecule
mol_deba9ee853427011 and the omeprazole molecule mol_3e8a906cc685122f. The
deterministic pass is tested on them directly; the judged pass is tested
through its typed output, so no model call is made and nothing is faked in
place of one.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from pipeline.MedicareProviderEvaluationDataPipeline.medicare_indication_canonicalizer import (
    CanonicalIndication,
    CanonicalizeOutput,
    canonicalize_molecule,
    collapse_identical_code_sets,
    every_wording_is_accounted_for,
)


def _entry(text, level3, level4, trust=None):
    return {"raw_text": text,
            "icd10": {"level3": list(level3), "level4": list(level4)},
            "_crosswalk_provenance": "inference",
            "trust": None if trust is None else Decimal(str(trust))}


# The three common-cold wordings from mol_deba9ee853427011, all resolving to
# level3 [J00] with no level4 -- the same indication three times.
COMMON_COLD = [
    _entry("Common cold", ["J00"], [], "0.8"),
    _entry("Common cold with nasal congestion", ["J00"], [], "0.88"),
    _entry("Common-cold nasal congestion", ["J00"], [], "0.62"),
]

# Two wordings that resolve differently, so the deterministic pass cannot
# join them.
DIFFERENT_CODES = [
    _entry("Nasal congestion", ["R09"], ["R098"], "0.68"),
    _entry("Nasal or sinus congestion", ["J34"], ["J348"], "0.4"),
]


def test_identical_code_sets_collapse_to_one_indication():
    out = collapse_identical_code_sets(COMMON_COLD)
    assert len(out) == 1
    assert out[0]["icd10"] == {"level3": ["J00"], "level4": []}
    assert out[0]["source_texts"] == [
        "Common cold", "Common cold with nasal congestion",
        "Common-cold nasal congestion"]


def test_the_collapsed_entry_carries_the_lowest_trust():
    out = collapse_identical_code_sets(COMMON_COLD)
    assert out[0]["trust"] == Decimal("0.62")


def test_entries_with_different_code_sets_are_not_collapsed():
    out = collapse_identical_code_sets(DIFFERENT_CODES)
    assert len(out) == 2


def test_the_deterministic_pass_needs_no_model():
    result = canonicalize_molecule(COMMON_COLD, molecule="phenylephrine")
    assert result["judged"] == 0
    assert result["collapsed_deterministically"] == 1
    assert len(result["indications"]) == 1


def test_no_wording_is_lost_or_duplicated_by_the_deterministic_pass():
    result = canonicalize_molecule(COMMON_COLD + DIFFERENT_CODES,
                                   molecule="phenylephrine")
    assert result["lost"] == []
    assert result["duplicated"] == []


def test_every_wording_is_accounted_for_detects_a_lost_wording():
    canonical = [{"source_texts": ["Common cold"]}]
    lost, duplicated = every_wording_is_accounted_for(COMMON_COLD, canonical)
    assert "Common-cold nasal congestion" in lost
    assert duplicated == set()


def test_every_wording_is_accounted_for_detects_a_duplicated_wording():
    canonical = [{"source_texts": ["Common cold"]},
                 {"source_texts": ["Common cold"]}]
    _lost, duplicated = every_wording_is_accounted_for(
        [COMMON_COLD[0]], canonical)
    assert duplicated == {"Common cold"}


class _Canonicalizer:
    """Stands in for the model call by returning its typed output.

    This is not a fake of the model: it is the agent's own output_type,
    constructed by the test, which is the only way to exercise the code that
    consumes it without making a paid call.
    """

    def __init__(self, output: CanonicalizeOutput) -> None:
        self._output = output
        self.calls = 0

    def canonicalize(self, payload):
        self.calls = self.calls + 1
        self.payload = payload
        return self._output


def test_the_judged_pass_groups_what_the_codes_could_not():
    judged = CanonicalizeOutput(indications=[CanonicalIndication(
        name="Nasal congestion",
        level3=["J34", "R09"], level4=["J348", "R098"], trust="0.4",
        source_texts=["Nasal congestion", "Nasal or sinus congestion"])])
    canonicalizer = _Canonicalizer(judged)
    result = canonicalize_molecule(DIFFERENT_CODES, molecule="phenylephrine",
                                   canonicalizer=canonicalizer)
    assert canonicalizer.calls == 1
    assert result["judged"] == 1
    assert len(result["indications"]) == 1
    assert result["indications"][0]["name"] == "Nasal congestion"
    assert result["lost"] == []
    assert result["duplicated"] == []


def test_a_judged_grouping_that_drops_a_wording_is_reported():
    judged = CanonicalizeOutput(indications=[CanonicalIndication(
        name="Nasal congestion", level3=["R09"], level4=["R098"],
        source_texts=["Nasal congestion"])])
    result = canonicalize_molecule(DIFFERENT_CODES, molecule="phenylephrine",
                                   canonicalizer=_Canonicalizer(judged))
    assert result["lost"] == ["Nasal or sinus congestion"]


def test_one_entry_is_never_sent_to_the_model():
    canonicalizer = _Canonicalizer(CanonicalizeOutput(indications=[]))
    result = canonicalize_molecule([COMMON_COLD[0]], molecule="x",
                                   canonicalizer=canonicalizer)
    assert canonicalizer.calls == 0
    assert len(result["indications"]) == 1


def test_the_canonicalizer_refuses_to_pick_its_own_model():
    from pipeline.MedicareProviderEvaluationDataPipeline.medicare_indication_canonicalizer import (
        IndicationCanonicalizer)
    from chathealthy_lib.exceptions import ChatHealthyException
    with pytest.raises(ChatHealthyException) as caught:
        IndicationCanonicalizer(model=None)._require_model_id()
    assert caught.value.mode == "config_error"
