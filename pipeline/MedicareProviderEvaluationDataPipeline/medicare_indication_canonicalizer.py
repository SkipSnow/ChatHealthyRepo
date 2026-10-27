# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Canonical indications: one record per clinical concept, not per wording.

A molecule folds many FDA labels and each label words its indications its own
way, so the fold stores the same clinical concept many times over. Measured
2026-10-26: one phenylephrine molecule carried 50+ indication entries for
about three conditions, and omeprazole carried 145 over 133 labels, several
resolving to byte-identical ICD-10 code sets.

TWO PASSES, AND THE CHEAP ONE RUNS FIRST.

  1 Deterministic. Entries whose level3 and level4 code sets are identical are
    the same indication by construction: one entry is kept and every collapsed
    wording moves into its source_texts. No model, no cost, no variance.

  2 Judged. What remains are wordings that mean the same thing but crosswalked
    to different code sets, which no comparison of the record can settle. One
    pydantic-ai Agent call per molecule reads the remaining entries and returns
    canonical indications, each with a plain-language name, its code set, its
    trust, and the source_texts it absorbed.

Nothing is dropped by either pass: every input wording appears in exactly one
canonical entry's source_texts, which is what the tests assert.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pydantic import BaseModel, ConfigDict, Field

_log = ChatHealthyLoggingService()

_MODEL_ENV = "MEDICARE_INDICATION_MODEL"


# ── the typed input and output (Rule-065 statement 11) ───────────────────────
class IndicationEntry(BaseModel):
    """One indication as the fold produced it."""
    model_config = ConfigDict(extra="forbid")
    raw_text: str
    level3: list[str] = Field(default_factory=list)
    level4: list[str] = Field(default_factory=list)
    trust: Optional[str] = None


class CanonicalizeInput(BaseModel):
    """The deps the agent is handed: one molecule and its candidates."""
    model_config = ConfigDict(extra="forbid")
    molecule: str
    entries: list[IndicationEntry] = Field(min_length=1)


class CanonicalIndication(BaseModel):
    """One clinical concept, with every wording that expressed it."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1)
    level3: list[str] = Field(default_factory=list)
    level4: list[str] = Field(default_factory=list)
    trust: Optional[str] = None
    source_texts: list[str] = Field(min_length=1)


class CanonicalizeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    indications: list[CanonicalIndication]


_SYSTEM_PROMPT = (
    "You are given one drug molecule and the indication wordings taken from "
    "its FDA labels, each with the ICD-10-CM codes a prior step resolved it "
    "to. Group the wordings that denote the SAME clinical concept into one "
    "canonical indication.\n"
    "For each canonical indication return: name, a short plain-language name "
    "for the concept; level3 and level4, the union of the ICD-10-CM codes of "
    "the wordings you grouped; trust, the lowest trust among them; and "
    "source_texts, every input wording you grouped, verbatim.\n"
    "Rules. Group only wordings that denote the same clinical concept - a "
    "different site, a different population or a different disease is a "
    "different indication. Every input wording must appear in exactly one "
    "canonical indication's source_texts: none may be dropped and none may "
    "appear twice. Invent no code that is not in the input."
)


def _code_key(entry: dict) -> tuple:
    """The identity of an entry's resolved code set."""
    icd10 = entry.get("icd10") or {}
    return (tuple(sorted(icd10.get("level3") or [])),
            tuple(sorted(icd10.get("level4") or [])))


def _lowest_trust(entries: list[dict]):
    trusts = [e.get("trust") for e in entries if e.get("trust") is not None]
    return min(trusts) if trusts else None


def collapse_identical_code_sets(entries: list[dict]) -> list[dict]:
    """Pass 1: entries resolving to the same codes are one indication.

    Deterministic and free. The first wording seen names the entry and every
    other wording of the group moves into source_texts, so the count falls
    without a model and without losing a word.
    """
    groups: dict[tuple, list[dict]] = {}
    order: list[tuple] = []
    for entry in entries:
        key = _code_key(entry)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)

    out: list[dict] = []
    for key in order:
        group = groups[key]
        first = group[0]
        icd10 = first.get("icd10") or {}
        texts: list[str] = []
        for member in group:
            for text in ([member.get("raw_text")]
                         + list(member.get("source_texts") or [])):
                if text and text not in texts:
                    texts.append(text)
        out.append({
            "raw_text": first.get("raw_text"),
            "icd10": {"level3": list(icd10.get("level3") or []),
                      "level4": list(icd10.get("level4") or [])},
            "_crosswalk_provenance": first.get("_crosswalk_provenance"),
            "trust": _lowest_trust(group),
            "source_texts": texts,
        })
    return out


class IndicationCanonicalizer:
    """Pass 2: the judged grouping, one Agent call per molecule."""

    def __init__(self, *, model: str | None = None,
                 retries: int = 2) -> None:
        self._model_id = model or (os.environ.get(_MODEL_ENV, "").strip()
                                   or None)
        self._retries = retries
        self._agent = None

    def _require_model_id(self) -> str:
        if not self._model_id:
            raise ChatHealthyException(
                mode="config_error",
                component="IndicationCanonicalizer",
                message=(f"no model configured: pass model= or set "
                         f"{_MODEL_ENV}; the canonicalizer never picks one "
                         f"for itself"))
        return self._model_id

    def _get_agent(self):
        if self._agent is None:
            from pydantic_ai import Agent  # noqa: PLC0415
            self._agent = Agent(
                self._require_model_id(),
                deps_type=CanonicalizeInput,
                output_type=CanonicalizeOutput,
                system_prompt=_SYSTEM_PROMPT,
                retries=self._retries,
            )
        return self._agent

    def canonicalize(self, payload: CanonicalizeInput) -> CanonicalizeOutput:
        """One molecule's remaining entries, grouped by clinical concept."""
        agent = self._get_agent()
        result = agent.run_sync(
            "Molecule: " + payload.molecule + "\n"
            + payload.model_dump_json(include={"entries"}),
            deps=payload)
        return result.output


def every_wording_is_accounted_for(entries: list[dict],
                                   canonical: list[dict]) -> tuple[set, set]:
    """(lost, duplicated) wordings. Both empty is the only acceptable result."""
    before: list[str] = []
    for entry in entries:
        for text in ([entry.get("raw_text")]
                     + list(entry.get("source_texts") or [])):
            if text:
                before.append(text)
    after: list[str] = []
    for entry in canonical:
        after.extend(t for t in (entry.get("source_texts") or []) if t)
    lost = set(before) - set(after)
    duplicated = {t for t in after if after.count(t) > 1}
    return lost, duplicated


def canonicalize_molecule(entries: list[dict], *, molecule: str,
                          canonicalizer: Any | None = None) -> dict:
    """Both passes over one molecule's indications.

    Returns {"indications", "collapsed_deterministically", "judged",
    "lost", "duplicated"}. With no canonicalizer the deterministic pass runs
    alone, which is the whole answer whenever it leaves one entry per code
    set.
    """
    deterministic = collapse_identical_code_sets(entries)
    judged = 0
    out = deterministic

    if canonicalizer is not None and len(deterministic) > 1:
        payload = CanonicalizeInput(
            molecule=molecule,
            entries=[IndicationEntry(
                raw_text=e.get("raw_text") or "",
                level3=list((e.get("icd10") or {}).get("level3") or []),
                level4=list((e.get("icd10") or {}).get("level4") or []),
                trust=None if e.get("trust") is None else str(e["trust"]))
                for e in deterministic])
        result = canonicalizer.canonicalize(payload)
        judged = len(result.indications)
        out = [{
            "raw_text": c.name,
            "name": c.name,
            "icd10": {"level3": list(c.level3), "level4": list(c.level4)},
            "_crosswalk_provenance": "inference",
            "trust": c.trust,
            "source_texts": list(c.source_texts),
        } for c in result.indications]

    lost, duplicated = every_wording_is_accounted_for(entries, out)
    return {"indications": out,
            "collapsed_deterministically": len(deterministic),
            "judged": judged,
            "lost": sorted(lost),
            "duplicated": sorted(duplicated)}
