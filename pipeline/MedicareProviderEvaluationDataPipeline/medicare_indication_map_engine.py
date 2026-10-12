# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Normalized Indication Map mining engine (EPIC-010-F-007).

The hierarchy is adopted from the published vocabularies, not manufactured: the
AHRQ CCSR staging gives, for every ICD-10-CM leaf code, the clinical category it
rolls to (parent_indication) and the body-system superclass that category sits
under (disease_class). One NormalizedIndication document is keyed by each CCSR
category. Molecule -> indication associations are mined across the WHOLE FDA
drug-label universe: every openFDA-labelled product carrying an rxcui and
indications_and_usage prose is resolved to ICD-10-CM codes, then joined to the
categories those codes belong to, so the map relates every indication to every
FDA-labelled drug and every ICD-10 code (EPIC-010-F-007-S-008-REQ-B-004). The
per-provider steps consume only their own subset of this complete map. The
inference runs through a pydantic-ai Agent directly (typed input, typed output);
the model is a config value (CH_INDICATION_MODEL, a pydantic-ai provider:model
string) so neither vendor nor model is pinned in source.
"""

from __future__ import annotations

import os
from typing import Any, Iterable

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

_MODEL_ENV = "CH_INDICATION_MODEL"
# The openFDA label universe is grouped to unique MOLECULE (the same molecule
# recurs across many products/strengths/brands/label versions) and mapped in
# batches of this size through one Agent call each, so the call count is
# (unique molecules / batch size).
_CROSSWALK_BATCH_SIZE = 50

# Standard AHRQ DXCCSR body-system superclasses, keyed by the CCSR category
# code's three-letter prefix. An unmapped prefix carries the raw prefix token
# rather than an invented name.
_BODY_SYSTEM = {
    "BLD": "Diseases of the blood and blood-forming organs",
    "CIR": "Diseases of the circulatory system",
    "DIG": "Diseases of the digestive system",
    "EAR": "Diseases of the ear and mastoid process",
    "END": "Endocrine, nutritional and metabolic diseases",
    "EYE": "Diseases of the eye and adnexa",
    "FAC": "Factors influencing health status",
    "GEN": "Diseases of the genitourinary system",
    "INF": "Certain infectious and parasitic diseases",
    "INJ": "Injury, poisoning and other external causes",
    "MAL": "Congenital malformations and chromosomal abnormalities",
    "MBD": "Mental, behavioral and neurodevelopmental disorders",
    "MUS": "Diseases of the musculoskeletal system and connective tissue",
    "NEO": "Neoplasms",
    "NVS": "Diseases of the nervous system",
    "PNL": "Certain conditions originating in the perinatal period",
    "PRG": "Pregnancy, childbirth and the puerperium",
    "RSP": "Diseases of the respiratory system",
    "SKN": "Diseases of the skin and subcutaneous tissue",
    "SYM": "Symptoms, signs and abnormal clinical findings",
    "EXT": "External causes of morbidity",
}


def _strip(v: Any) -> str:
    return str(v or "").strip().strip("'").strip()


def _norm_icd10(code: str) -> str:
    return _strip(code).upper().replace(".", "")


def _rows(coll, run_id: str) -> Iterable[dict]:
    for doc in coll.find({"run_id": run_id}):
        yield doc.get("raw") or {}


def _parse_ccsr_row(raw: dict) -> tuple[str, str, list[tuple[str, str]]]:
    """Return (icd10_code, icd10_desc, [(category_code, category_desc), ...]).

    AHRQ wraps header and value text in single quotes and carries up to six
    CCSR category assignments per code in numbered columns; this reads whatever
    numbered category/description pairs are present.
    """
    code = desc = ""
    cat_codes: dict[str, str] = {}
    cat_descs: dict[str, str] = {}
    for key, val in raw.items():
        k = _strip(key).lower()
        if not k:
            continue
        if "icd-10-cm code" in k or "icd10cm code" in k or k == "icd-10-cm_code":
            if "description" in k:
                desc = _strip(val)
            else:
                code = _norm_icd10(val)
        elif "ccsr category" in k or "ccsr_category" in k:
            digit = "".join(ch for ch in k if ch.isdigit()) or "1"
            if "description" in k:
                cat_descs[digit] = _strip(val)
            else:
                cat_codes[digit] = _strip(val)
    cats: list[tuple[str, str]] = []
    for digit, ccode in cat_codes.items():
        if ccode and ccode.upper() not in ("", "XXX000", "NA"):
            cats.append((ccode, cat_descs.get(digit, "")))
    return code, desc, cats


def _body_system(category_code: str) -> str:
    prefix = _strip(category_code)[:3].upper()
    return _BODY_SYSTEM.get(prefix, prefix)


def _crosswalk_prompt(batch: list[dict]) -> str:
    """The user message for one batch: each molecule's rxcui, ingredient and
    FDA-label indication prose, numbered. The model echoes each rxcui back."""
    parts: list[str] = []
    for n, m in enumerate(batch, 1):
        parts.append(
            f"{n}. rxcui={m['rxcui']} ingredient={m.get('ingredient') or ''}\n"
            f"indications: {m['prose']}")
    return ("Map each of the following drug molecules to the ICD-10-CM codes it "
            "is on-label indicated to treat:\n\n" + "\n\n".join(parts))


def _crosswalk_batch(batch: list[dict], *, model: str) -> dict[str, list[tuple[str, float]]]:
    """One pydantic-ai Agent call for a batch of molecules.

    Returns rxcui -> [(icd10_code, confidence), ...]. Reaches the model only
    through a pydantic_ai.Agent run directly, with a typed pydantic input
    (CrosswalkInput) and a typed pydantic output (CrosswalkOutput); no facade,
    no raw vendor SDK (Rule-065 statement 11). Every returned association carries
    a mandatory 0..1 confidence (the trust value); a best guess is acceptable and
    the confidence reflects how sure the model is."""
    from pydantic import BaseModel, Field
    from pydantic_ai import Agent
    from pydantic_ai.settings import ModelSettings

    class MoleculeInput(BaseModel):
        rxcui: str = Field(description="the molecule's RxNorm rxcui")
        ingredient: str = Field(default="", description="active ingredient name, if known")
        indications_text: str = Field(description="FDA-label Indications and Usage prose")

    class CrosswalkInput(BaseModel):
        molecules: list[MoleculeInput] = Field(description="the batch of molecules to map")

    class CodeConfidence(BaseModel):
        icd10_code: str = Field(description="an ICD-10-CM diagnosis code, no dots")
        confidence: float = Field(
            ge=0.0, le=1.0,
            description="mandatory 0..1 confidence the drug is on-label indicated "
                        "for this condition; a best guess is acceptable")

    class MoleculeResult(BaseModel):
        rxcui: str = Field(description="the rxcui this result is for, echoed from the input")
        codes: list[CodeConfidence] = Field(
            default_factory=list,
            description="the indicated ICD-10-CM codes, each with its confidence")

    class CrosswalkOutput(BaseModel):
        results: list[MoleculeResult] = Field(description="one entry per input molecule")

    agent = Agent(
        model,
        deps_type=CrosswalkInput,
        output_type=CrosswalkOutput,
        system_prompt=(
            "You map each FDA drug label's Indications and Usage prose to the "
            "ICD-10-CM diagnosis codes naming the on-label indicated conditions. "
            "For every molecule in the batch return its rxcui, echoed exactly, and "
            "the ICD-10-CM codes (no dots) it is indicated to treat, each carrying "
            "a confidence between 0 and 1. A best guess is acceptable -- "
            "approximate is fine, and the confidence reflects how sure you are. "
            "Every code MUST carry a confidence. If a molecule has no identifiable "
            "indication, return it with an empty code list."
        ),
        model_settings=ModelSettings(temperature=0),
    )
    deps = CrosswalkInput(molecules=[
        MoleculeInput(rxcui=m["rxcui"], ingredient=m.get("ingredient") or "",
                      indications_text=m["prose"])
        for m in batch])
    result = agent.run_sync(_crosswalk_prompt(batch), deps=deps)
    out = getattr(result, "output", None) or getattr(result, "data", None)
    mapping: dict[str, list[tuple[str, float]]] = {}
    for r in getattr(out, "results", None) or []:
        rx = _strip(getattr(r, "rxcui", ""))
        if not rx:
            continue
        pairs: list[tuple[str, float]] = []
        for c in getattr(r, "codes", None) or []:
            code = _norm_icd10(getattr(c, "icd10_code", ""))
            conf = getattr(c, "confidence", None)
            if code and conf is not None:
                pairs.append((code, float(conf)))
        mapping[rx] = pairs
    return mapping


def build_indication_map(
    *,
    run_id: str,
    data_version: int,
    ccsr_coll,
    icd10_coll,
    openfda_coll,
    target_coll,
    rebuild: bool,
    batch_size: int = 500,
) -> dict[str, Any]:
    """Mine the Normalized Indication Map into target_coll."""
    # 1. Hierarchy from CCSR: one indication per category, members its ICD-10 leaves.
    indications: dict[str, dict] = {}
    icd10_to_cats: dict[str, set] = {}
    ccsr_rows = 0
    for raw in _rows(ccsr_coll, run_id):
        code, _desc, cats = _parse_ccsr_row(raw)
        if not code or not cats:
            continue
        ccsr_rows += 1
        for ccode, cdesc in cats:
            ind = indications.setdefault(ccode, {
                "indication_id": ccode,
                "parent_indication": cdesc or ccode,
                "disease_class": _body_system(ccode),
                "member_icd10": {},     # icd10 -> justification_source
                "molecules": {},        # rxcui -> {ingredient, justification_source}
            })
            if cdesc and ind["parent_indication"] in ("", ccode):
                ind["parent_indication"] = cdesc
            ind["member_icd10"].setdefault(code, "CCSR")
            icd10_to_cats.setdefault(code, set()).add(ccode)

    if not indications:
        raise ChatHealthyException(
            mode="runtime_error",
            component="build_indication_map",
            message=(f"build_indication_map: no CCSR hierarchy mined for run_id={run_id}; "
                     "CCSR staging empty or column layout unrecognized"),
        )

    # 2. Molecule -> indication crosswalk via FDA label prose, across the whole
    # FDA label universe (every labelled product, not a prescribed subset). The
    # label rows are GROUPED BY MOLECULE (the active ingredient) -- not by the
    # product-level rxcui, which over-counts the same drug across strengths,
    # brands and label versions -- and all of a molecule's distinct
    # indications_and_usage prose is UNIONED into one combined text, mapped once.
    # A molecule legitimately yields many indications; all are attached, each
    # with its own 0..1 confidence. The grouping key is PROVISIONAL: the
    # normalized substance/generic ingredient name; proper RxNorm-ingredient
    # normalization is deferred (O2 / the MD-coder benchmark). Each molecule's
    # rxcui(s) are carried through so the map keys associations by rxcui as
    # today. Batched call count is (unique molecules / batch size).
    llm_calls = 0
    molecules_attached = 0
    if rebuild and openfda_coll is not None:
        model = os.environ.get(_MODEL_ENV, "").strip()
        if not model:
            raise ChatHealthyException(
                mode="config_error",
                component="build_indication_map",
                message=(f"{_MODEL_ENV} is not set; a rebuild mines the molecule "
                         "crosswalk through a pydantic-ai Agent and needs the model binding"),
            )
        by_molecule: dict[str, dict] = {}
        for raw in _rows(openfda_coll, run_id):
            of = raw.get("openfda") or {}
            subs = of.get("substance_name") or of.get("generic_name") or []
            prose = " ".join(raw.get("indications_and_usage") or []).strip()
            if not prose:
                continue
            ingredient = _strip(subs[0]) if subs else ""
            key = ingredient.lower()
            if not key:
                continue
            mol = by_molecule.setdefault(
                key, {"ingredient": ingredient, "rxcuis": set(),
                      "prose_parts": [], "prose_seen": set()})
            for rx in of.get("rxcui") or []:
                rxs = _strip(rx)
                if rxs:
                    mol["rxcuis"].add(rxs)
            # Union of DISTINCT prose: identical labels do not bloat the text,
            # but a product that lists a different indication keeps it.
            if prose not in mol["prose_seen"]:
                mol["prose_seen"].add(prose)
                mol["prose_parts"].append(prose)
        # Only molecules carrying at least one rxcui can key the map as today.
        molecules: list[dict] = []
        for mol in by_molecule.values():
            rxcuis = sorted(mol["rxcuis"])
            if not rxcuis:
                continue
            molecules.append({
                "rxcui": rxcuis[0],          # representative, for echo-matching
                "rxcuis": rxcuis,            # all products' rxcuis, to key the map
                "ingredient": mol["ingredient"],
                "prose": "\n\n".join(mol["prose_parts"]),
            })
        for start in range(0, len(molecules), _CROSSWALK_BATCH_SIZE):
            batch = molecules[start:start + _CROSSWALK_BATCH_SIZE]
            llm_calls += 1
            mapping = _crosswalk_batch(batch, model=model)
            mol_by_repr = {m["rxcui"]: m for m in batch}
            for repr_rx, pairs in mapping.items():
                mol = mol_by_repr.get(repr_rx)
                if not mol:
                    continue
                # Keep the highest code confidence per category the molecule's
                # codes roll to, so each molecule->indication association carries
                # one mandatory 0..1 trust value. Many categories per molecule is
                # expected (one drug, many indications); all are attached.
                cat_conf: dict[str, float] = {}
                for code, conf in pairs:
                    for ccode in icd10_to_cats.get(code, set()):
                        if conf > cat_conf.get(ccode, -1.0):
                            cat_conf[ccode] = conf
                for ccode, conf in cat_conf.items():
                    ind = indications.get(ccode)
                    if not ind:
                        continue
                    # Carried through to every rxcui of the molecule so each
                    # product joins by rxcui as today; produced by inference, so
                    # it carries the model's own mandatory 0..1 confidence.
                    for rx in mol["rxcuis"]:
                        if rx in ind["molecules"]:
                            continue
                        ind["molecules"][rx] = {
                            "ingredient": mol["ingredient"],
                            "justification_source": "fda_label",
                            "justification_kind": "llm_crosswalk",
                            "trust": round(conf, 4),
                        }
                        molecules_attached += 1

    # 4. Write one document per CCSR category.
    ops: list[UpdateOne] = []
    written = 0
    for ccode, ind in indications.items():
        doc = {
            "_id": ccode,
            "indication_id": ccode,
            "parent_indication": ind["parent_indication"],
            "disease_class": ind["disease_class"],
            "run_id": run_id,
            "data_version": data_version,
            "member_icd10": [
                {"icd10": c, "justification_source": src}
                for c, src in sorted(ind["member_icd10"].items())
            ],
            "molecules": [
                {"rxcui": rx, "ingredient": m["ingredient"],
                 "justification_source": m["justification_source"],
                 "justification_kind": m.get("justification_kind"),
                 "trust": m.get("trust")}
                for rx, m in sorted(ind["molecules"].items())
            ],
        }
        ops.append(UpdateOne({"_id": ccode}, {"$set": doc}, upsert=True))
        if len(ops) >= batch_size:
            target_coll.bulk_write(ops, ordered=False)
            written += len(ops)
            ops = []
    if ops:
        target_coll.bulk_write(ops, ordered=False)
        written += len(ops)

    _log.LogPipeline(
        "INFO",
        "build_indication_map: categories=%d ccsr_rows=%d "
        "llm_calls=%d molecules_attached=%d written=%d rebuild=%s",
        len(indications), ccsr_rows, llm_calls,
        molecules_attached, written, rebuild,
    )
    return {
        "indications_written": written,
        "ccsr_rows": ccsr_rows,
        "llm_calls": llm_calls,
        "molecules_attached": molecules_attached,
        "rebuild": rebuild,
    }
