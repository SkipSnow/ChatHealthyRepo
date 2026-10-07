# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Normalized Indication Map mining engine (EPIC-010-F-007).

The hierarchy is adopted from the published vocabularies, not manufactured: the
AHRQ CCSR staging gives, for every ICD-10-CM leaf code, the clinical category it
rolls to (parent_indication) and the body-system superclass that category sits
under (disease_class). One NormalizedIndication document is keyed by each CCSR
category. Molecule -> indication associations are mined: the molecule universe is
the set of drugs actually prescribed under Part D, and each molecule's FDA-label
indications_and_usage prose is resolved to ICD-10-CM codes through the shared LLM
facade (the thin residual), then joined to the categories those codes belong to.
The model is a config value (CH_INDICATION_MODEL, a pydantic-ai provider:model
string) so neither vendor nor model is pinned in source.
"""

from __future__ import annotations

import math
import os
from typing import Any, Iterable

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from chathealthy_lib.llm import run_llm_sync
from pymongo import UpdateOne

_log = ChatHealthyLoggingService()

_MODEL_ENV = "CH_INDICATION_MODEL"
_PARTD_GENERIC = "Gnrc_Name"

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


def _logprob_settings(model: str) -> dict:
    """Model settings that ask the provider to return token logprobs. The trust
    score is the inference's own confidence (avg logprobs), never a number the
    model is asked to invent; the provider must expose logprobs (Gemini/OpenAI,
    not Anthropic)."""
    s: dict[str, Any] = {"temperature": 0, "max_tokens": 4000}
    m = model.lower()
    if m.startswith(("google", "gemini")):
        s["google_logprobs"] = True
    elif m.startswith(("openai", "gpt")):
        s["openai_logprobs"] = True
    return s


def _trust_from_result(result) -> float | None:
    """The call's average token logprob, mapped to a 0..1 confidence (exp of the
    mean per-token logprob). None when the provider returned no logprobs."""
    resp = getattr(result, "response", None)
    pd = getattr(resp, "provider_details", None) if resp is not None else None
    if not pd:
        return None
    avg = pd.get("avg_logprobs")
    if avg is None:
        return None
    try:
        return round(min(1.0, max(0.0, math.exp(float(avg)))), 4)
    except (TypeError, ValueError, OverflowError):
        return None


def _residual_icd10_codes(prose: str, *, model: str, call_site: str) -> tuple[list[str], float | None]:
    """Thin residual: FDA-label indication prose -> (ICD-10-CM codes, trust score).

    The trust score is the inference's own logprob confidence, carried on the
    molecule->indication facts this call produces. Not batched: one call per
    molecule, so the score is a clean per-molecule confidence."""
    from pydantic import BaseModel, Field
    from pydantic_ai import Agent
    from pydantic_ai.settings import ModelSettings

    class IcdCodes(BaseModel):
        icd10_codes: list[str] = Field(default_factory=list)

    agent = Agent(
        model,
        output_type=IcdCodes,
        system_prompt=(
            "You map an FDA drug label's Indications and Usage prose to the "
            "ICD-10-CM diagnosis codes naming the on-label indicated conditions. "
            "Return only ICD-10-CM codes (no dots) for conditions the drug is "
            "indicated to treat. If none can be identified, return an empty list."
        ),
        model_settings=ModelSettings(**_logprob_settings(model)),
    )
    provider = model.split(":", 1)[0] if ":" in model else model
    result = run_llm_sync(
        agent, prose, call_site=call_site, provider=provider,
        server="pipeline", component="build_indication_map",
    )
    out = getattr(result, "output", None) or getattr(result, "data", None)
    codes = getattr(out, "icd10_codes", None) or []
    return [_norm_icd10(c) for c in codes if _strip(c)], _trust_from_result(result)


def build_indication_map(
    *,
    run_id: str,
    data_version: int,
    ccsr_coll,
    icd10_coll,
    partd_coll,
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

    # 2. Molecule universe: the drugs actually prescribed under Part D.
    prescribed: set[str] = set()
    for raw in _rows(partd_coll, run_id):
        g = _strip(raw.get(_PARTD_GENERIC)).lower()
        if g:
            prescribed.add(g)

    # 3. Molecule -> indication crosswalk via FDA label prose (rebuild only).
    residual_calls = 0
    molecules_attached = 0
    if rebuild and openfda_coll is not None:
        model = os.environ.get(_MODEL_ENV, "").strip()
        if not model:
            raise ChatHealthyException(
                mode="config_error",
                component="build_indication_map",
                message=(f"{_MODEL_ENV} is not set; a rebuild mines the molecule "
                         "crosswalk through the facade and needs the model binding"),
            )
        for raw in _rows(openfda_coll, run_id):
            of = raw.get("openfda") or {}
            subs = of.get("substance_name") or of.get("generic_name") or []
            rxcuis = of.get("rxcui") or []
            if not rxcuis:
                continue
            rxcui = _strip(rxcuis[0])
            ingredient = _strip(subs[0]) if subs else None
            if not any(_strip(s).lower() in prescribed for s in subs):
                continue
            prose = " ".join(raw.get("indications_and_usage") or []).strip()
            if not prose or not rxcui:
                continue
            residual_calls += 1
            codes, trust_score = _residual_icd10_codes(
                prose, model=model, call_site=f"build_indication_map:{rxcui}")
            cats: set = set()
            for c in codes:
                cats |= icd10_to_cats.get(c, set())
            for ccode in cats:
                ind = indications.get(ccode)
                if not ind or rxcui in ind["molecules"]:
                    continue
                # This association is produced by inference, so it carries a
                # trust score (the call's logprob confidence); deterministic
                # facts carry none.
                ind["molecules"][rxcui] = {
                    "ingredient": ingredient,
                    "justification_source": "fda_label",
                    "justification_kind": "residual-LLM",
                    "trust": trust_score,
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
        "build_indication_map: categories=%d ccsr_rows=%d prescribed_molecules=%d "
        "residual_calls=%d molecules_attached=%d written=%d rebuild=%s",
        len(indications), ccsr_rows, len(prescribed), residual_calls,
        molecules_attached, written, rebuild,
    )
    return {
        "indications_written": written,
        "ccsr_rows": ccsr_rows,
        "prescribed_molecules": len(prescribed),
        "residual_calls": residual_calls,
        "molecules_attached": molecules_attached,
        "rebuild": rebuild,
    }
