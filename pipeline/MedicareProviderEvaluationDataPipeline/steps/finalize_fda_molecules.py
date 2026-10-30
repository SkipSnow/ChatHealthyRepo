# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""finalize_fda_molecules (EPIC-010-F-007, LLD v83 III.10) -- the serial
molecule finalize, the second of the two formal FDA build steps (operator
ruling 2026-10-20).

Groups the per-label resolution results (the transient collection the fan-out
step wrote) by molecule_id and writes fdaProductToDiagnosisResolution at the
MOLECULE grain: one document per molecule, _id = molecule_id, carrying the
folded labels' set_ids (the contributing record keys) and the unioned FDA
facts (LLD v83 III.4.1). Single-writer and exactly-once by construction: the
group-by IS the fold, and the write is a deterministic full-document upsert.

Full annual replace (decision #1): this data_version's published collection is
cleared before writing, so no residue from prior or aborted runs survives.

A label whose molecule cannot be minted (no clean name recovered) cannot fold
into any molecule: its transient row is stamped orphan: true with the reason
and a finding is recorded -- NON-FATAL per hard-fail ruling H2; the
conformance step reports the arithmetic.

Molecule identity is enriched by a LOCAL join against the staged RxNorm
vocabulary (LLD v83 III.9/III.10): the run's staged RXNCONSO ingredient rows
(TTY IN/PIN) are folded by the same molecule_id normalization and their RXCUIs
unioned into molecule.rxcui. No external terminology API is called at run time
-- the former RxNav usage was an off-design bug, removed (operator ruling
2026-10-20).
"""

from __future__ import annotations

import datetime
import os

from chathealthy_lib.exceptions import ChatHealthyException
from chathealthy_lib.logging_service import ChatHealthyLoggingService
from pymongo.errors import BulkWriteError

from pipeline.run_lifecycle.pipeline_runtime import PipelineRuntime
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (
    _trust_stats,
    _bson, fda_molecule_union, molecule_id)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_indication_canonicalizer import (
    IndicationCanonicalizer, canonicalize_molecule)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_rxnorm_identity import (
    build_index, resolve_identity)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_store_contract import (
    PUBLISHED_CONTRACTS, load_contract, json_view, record_refused_writes,
    validator_for)
from pipeline.MedicareProviderEvaluationDataPipeline.steps.build_fda_label_resolution import (
    TRANSIENT_COLLECTION)

_log = ChatHealthyLoggingService()

# RXNCONSO term types that name an ingredient: IN = ingredient, PIN = precise
# ingredient. Only these rows carry molecule-grain identity; every other TTY
# (brand, clinical drug, pack...) is a different grain and is not joined.
_RXNORM_INGREDIENT_TTYS = ("IN", "PIN")


def _rxnorm_rxcui_by_molecule(ctx, reg, mongo) -> dict[str, list[str]]:
    """The staged RxNorm vocabulary folded to the pipeline's molecule grain:
    molecule_id(STR) -> first-seen-ordered RXCUI list, from the staged
    RXNCONSO ingredient rows (TTY IN/PIN). A LOCAL join -- the vocabulary is a
    declared, fetched, staged source like every other (LLD v83 III.9). An
    empty staging in build mode is a missing source: hard fail (H6)."""
    rx = reg.by_source_name("rxnorm")
    coll = mongo[rx.staging_db][reg.staging_collection_name("rxnorm")]
    by_mid: dict[str, list[str]] = {}
    rows = 0
    # No run_id filter: the drained-and-replaced staging is the one truth
    # (Start At, operator design 2026-10-22). The loader stages every source's
    # columns under "raw" (staging_loader LLD 4.6) -- top-level TTY/RXCUI/STR
    # can never match; that mis-path killed run 41aa1028 on this step's first
    # ever execution (2026-10-22).
    cursor = coll.find(
        {"raw.TTY": {"$in": list(_RXNORM_INGREDIENT_TTYS)}},
        {"raw.RXCUI": 1, "raw.STR": 1})
    for row in cursor:
        rows += 1
        raw = row.get("raw") or {}
        mid = molecule_id(raw.get("STR"))
        rxcui = str(raw.get("RXCUI") or "").strip()
        if not mid or not rxcui:
            continue
        dest = by_mid.setdefault(mid, [])
        if rxcui not in dest:
            dest.append(rxcui)
    if rows == 0:
        raise ChatHealthyException(
            mode="runtime_error",
            component="finalize_fda_molecules",
            message=("finalize_fda_molecules: the staged RxNorm vocabulary holds "
                     "zero ingredient rows (TTY IN/PIN) -- the rxnorm source is "
                     "declared and required; missing source is a hard fail (H6)"))
    _log.LogPipeline(
        "INFO", "finalize_fda_molecules: rxnorm vocabulary rows=%d molecules=%d",
        rows, len(by_mid))
    return by_mid


def _conformance_errors(doc: dict, validator) -> list[str]:
    """Every way this document fails the published contract, or none."""
    return [f"{'.'.join(str(p) for p in e.absolute_path) or '<root>'}: "
            f"{e.message}"
            for e in validator.iter_errors(json_view(doc))]


def _insert_conforming(coll, docs: list[dict], keys: list[str], *,
                       record_discrepancy, validator=None) -> int:
    """Write the documents that conform; hold the ones that do not.

    A non-compliant record is never offered to the store. It is checked
    against the published contract HERE and, failing, held in keyNotFound
    with the reason -- the same place a label with no key is held, because
    it is the same fact: this cannot be published, and it is kept to be
    managed rather than lost.

    This was always the rule and the code never honoured it. The store was
    the first thing to discover non-compliance: on 2026-10-29 one molecule
    of 20,142 carried a trust of int 1 against a contract pinning decimal,
    MongoDB refused it with code 121, the finding fired at severity fatal,
    and a run 3h24m old ended with its collection already dropped. Checked
    first, that molecule would have been held and the other 20,141
    published.

    UNORDERED on the insert that remains, so a document the store refuses
    anyway -- a refusal is now a genuine anomaly, not a routine event --
    does not abandon the ones that conform.
    """
    conforming: list[dict] = []
    conforming_keys: list[str] = []
    for doc, key in zip(docs, keys):
        errors = _conformance_errors(doc, validator) if validator else []
        if errors:
            record_discrepancy(
                reason="FDA_all_other_Attribute_failures",
                step="finalize_fda_molecules", artifact="fda",
                record_key=key,
                detail={"kind": "record_not_contract_conforming",
                        "errors": errors[:5]})
            continue
        conforming.append(doc)
        conforming_keys.append(key)
    if not conforming:
        return 0
    try:
        coll.insert_many(conforming, ordered=False)
        return len(conforming)
    except BulkWriteError as exc:
        refused = record_refused_writes(
            details=exc.details, keys=conforming_keys, artifact="fda",
            step="finalize_fda_molecules",
            record_discrepancy=record_discrepancy, exception=exc)
        return len(conforming) - refused


def _staged_rows(coll, projection: dict, page_size: int = 20000):
    """Every staged row of one source, paged by key so no cursor is held."""
    last_id = None
    while True:
        query = {} if last_id is None else {"_id": {"$gt": last_id}}
        page = list(coll.find(query, {**projection, "_id": 1},
                              sort=[("_id", 1)], limit=page_size))
        if not page:
            return
        last_id = page[-1]["_id"]
        yield from page


def _identity_index(ctx, reg, mongo):
    """The identity ladder's index from the run's own staged RxNorm rows.

    Both members are staged sources like any other, so this is a local read.
    RXNCONSO supplies the brand and ingredient names and the UNII; RXNREL
    supplies the brand-to-ingredient relationships.
    """
    conso_src = reg.by_source_name("rxnorm")
    conso = mongo[conso_src.staging_db][
        reg.staging_collection_name("rxnorm")]
    rel_src = reg.by_source_name("rxnorm_rel")
    rel = mongo[rel_src.staging_db][
        reg.staging_collection_name("rxnorm_rel")]

    index = build_index(
        _staged_rows(conso, {"raw.RXCUI": 1, "raw.SAB": 1, "raw.TTY": 1,
                             "raw.CODE": 1, "raw.STR": 1}),
        _staged_rows(rel, {"raw.RXCUI1": 1, "raw.RXCUI2": 1, "raw.RELA": 1}))

    if not index.brand_by_name or not index.ingredient_by_name:
        raise ChatHealthyException(
            mode="runtime_error",
            component="finalize_fda_molecules",
            message=("finalize_fda_molecules: the staged RxNorm vocabulary "
                     "yielded no brand or ingredient names; the full monthly "
                     "release is a declared and required source"))
    _log.LogPipeline(
        "INFO",
        "finalize_fda_molecules: identity index conso_rows=%d rel_rows=%d "
        "ingredients=%d brands=%d unii=%d",
        index.conso_rows, index.rel_rows, len(index.ingredient_by_name),
        len(index.brand_by_name), len(index.unii_by_rxcui))
    return index


def run_step(ctx) -> dict:
    rt = PipelineRuntime(ctx)
    reg = rt.registry
    mongo = ctx.mongo_client
    data_version = int(ctx.args.data_version)

    tgt = reg.by_source_name("fda_drug")
    target_coll = mongo[tgt.public_data_db][reg.public_data_collection_name("fda_drug")]

    building = bool(ctx.args.build_fda_drug)
    if not building:
        published = target_coll.count_documents({})
        if published == 0:
            raise ChatHealthyException(
                mode="runtime_error",
                component="finalize_fda_molecules",
                message=("finalize_fda_molecules: the run did not ask to build the "
                         "fdaProductToDiagnosisResolution collection and none is published "
                         "to consume; fire with --build-fda-drug to build it"))
        _log.LogPipeline(
            "INFO", "finalize_fda_molecules: consume mode; published molecules=%d",
            published)
        return {"step": "finalize_fda_molecules", "rebuild": False, "consumed": published}

    ofda = reg.by_source_name("openfda_labels")
    transient_coll = mongo[ofda.staging_db][TRANSIENT_COLLECTION]

    # Group the run's label results by molecule_id. Labels whose molecule
    # cannot be minted are orphans: stamped in the transient (operationally
    # queryable, H2) and recorded as findings -- never a run kill.
    by_molecule: dict[str, list[dict]] = {}
    orphans = 0
    labels = 0
    # Held across the molecule writes below. The FDA collection costs real
    # money to rebuild, so losing this loop to the cursor reaper is
    # expensive as well as slow.
    with transient_coll.find({}, no_cursor_timeout=True) as cursor:
     for lr in cursor:
         labels += 1
         mid = molecule_id(lr.get("molecule"))
         if not mid:
             orphans += 1
             transient_coll.update_one(
                 {"_id": lr["_id"]},
                 {"$set": {"orphan": True,
                           "orphan_reason": "no_molecule_id: no clean molecule "
                                            "name was recovered for this label"}})
             rt.record_discrepancy(
                 reason="FDA_all_other_Attribute_failures",
                 step="finalize_fda_molecules", artifact="fda",
                 record_key=str(lr.get("set_id") or lr.get("_id") or ""),
                 detail={"kind": "label_orphaned_no_molecule"})
             continue
         by_molecule.setdefault(mid, []).append(lr)

    # The LOCAL RxNorm identity join (LLD v83 III.9/III.10): the staged
    # vocabulary's ingredient RXCUIs, folded by the same molecule_id.
    rxnorm_by_mid = _rxnorm_rxcui_by_molecule(ctx, reg, mongo)

    # The identity ladder's index, built once from the staged RXNCONSO and
    # RXNREL rows: the brand and ingredient name lookups, the brand-to-
    # ingredient relationships, and the UNII each ingredient carries.
    identity_index = _identity_index(ctx, reg, mongo)

    # Full annual replace (decision #1), and the contract installed as the
    # collection's validator. The contract is read BEFORE the drop, so an
    # untranslatable contract stops the step with the collection still
    # standing; the validator is installed at creation, because one added
    # after records exist governs nothing already written.
    validator = validator_for("fda_drug")
    removed = target_coll.estimated_document_count()
    target_coll.drop()
    target_coll.database.create_collection(
        target_coll.name, validator=validator,
        validationAction="error", validationLevel="strict")
    target_coll = target_coll.database[target_coll.name]

    built_at = datetime.datetime.utcnow().isoformat()
    written = 0
    rxnorm_joined = 0
    resolved = 0
    unresolved = 0
    # The published contract, read once, as a validator this step applies
    # BEFORE offering anything to the store. The store's own validator
    # stays installed and is the backstop; this is the rule that a
    # non-compliant record is never written in the first place.
    from jsonschema import Draft202012Validator  # noqa: PLC0415
    contract_validator = Draft202012Validator(
        load_contract(PUBLISHED_CONTRACTS["fda_drug"]))

    pending: list[dict] = []
    pending_keys: list[str] = []
    no_indication = 0
    no_icd10 = 0
    collapsed = 0
    judged = 0
    # The canonicalizer runs its judged pass only when a model is configured;
    # the deterministic collapse runs either way.
    canonicalizer = (IndicationCanonicalizer()
                     if os.environ.get("MEDICARE_INDICATION_MODEL", "").strip()
                     else None)
    for mid, group in by_molecule.items():
        doc = fda_molecule_union(group)

        # Canonical indications: the deterministic collapse always, and the
        # judged grouping when a model is configured. Every wording survives
        # in exactly one entry's source_texts, and a grouping that loses or
        # duplicates one is recorded rather than published quietly.
        canonical = canonicalize_molecule(
            doc.get("indications") or [],
            molecule=(doc.get("molecule") or {}).get("clean_names", [mid])[0]
            if (doc.get("molecule") or {}).get("clean_names") else mid,
            canonicalizer=canonicalizer)
        doc["indications"] = canonical["indications"]
        # The trust block is a distribution OVER the indications beside
        # it, so it is recomputed after the collapse. fda_molecule_union
        # computed it over the pre-canonical set, and the collapse keeps
        # only the lowest trust of each group -- 50-odd wordings folding
        # to 3 on one phenylephrine molecule -- so the published block
        # described a larger population than the published indications.
        doc["trust"] = _trust_stats(
            [i["trust"] for i in doc["indications"]
             if i.get("trust") is not None])
        collapsed += canonical["collapsed_deterministically"]
        judged += canonical["judged"]
        if canonical["lost"] or canonical["duplicated"]:
            rt.record_discrepancy(
                reason="indication_without_icd10",
                step="finalize_fda_molecules", artifact="fda",
                record_key=mid,
                detail={"kind": "canonicalization_lost_a_wording",
                        "lost": canonical["lost"][:10],
                        "duplicated": canonical["duplicated"][:10]})
        joined = rxnorm_by_mid.get(mid)
        if joined:
            rxnorm_joined += 1
            have = set(doc["molecule"]["rxcui"])
            doc["molecule"]["rxcui"].extend(
                c for c in joined if c not in have)
        # The external identity, by the ladder: the label's own brand names
        # first, then its generic names. A molecule that resolves to neither
        # carries no identity field and is recorded as a finding.
        mol = doc.get("molecule") or {}
        identity = {"rxcui_in": None, "unii": None, "path": None, "also": []}
        # Every brand AND every generic. The generic was pinned at index 0
        # across all iterations, so a molecule carrying several generic
        # names only ever offered the ladder the first: 2,123 molecules
        # resolved to no identity in run 39747592.
        generics = mol.get("label_generic_names") or [None]
        for brand in (mol.get("label_brand_names") or [None]):
            for generic in generics:
                identity = resolve_identity(
                    identity_index, brand_name=brand,
                    generic_name=generic)
                if identity["rxcui_in"]:
                    break
            if identity["rxcui_in"]:
                break
        if identity["rxcui_in"]:
            resolved += 1
            doc["rxcui_in"] = identity["rxcui_in"]
            if identity["unii"]:
                doc["unii"] = identity["unii"]
            doc["identity_path"] = identity["path"]
            if identity["also"]:
                doc["identity_also"] = identity["also"]
        else:
            unresolved += 1
            rt.record_discrepancy(
                reason="drug_without_rxnorm_molecule",
                step="finalize_fda_molecules", artifact="fda",
                record_key=(mol.get("clean_names") or [mid])[0],
                detail={"kind": "no_rxnorm_identity",
                        "brand_names": mol.get("label_brand_names") or [],
                        "generic_names": mol.get("label_generic_names") or []})
        # A molecule carrying no indication at all, and an indication that
        # resolved to no diagnosis code, are both recorded so the counts reach
        # the run report instead of being silent.
        if not doc.get("indications"):
            no_indication += 1
            rt.record_discrepancy(
                reason="molecule_without_indication",
                step="finalize_fda_molecules", artifact="fda",
                record_key=(mol.get("clean_names") or [mid])[0],
                detail={"kind": "no_indication_on_any_label",
                        "set_ids": len(doc.get("set_ids") or [])})
        for ind in doc.get("indications") or []:
            codes = (ind.get("icd10") or {})
            if not (codes.get("level3") or codes.get("level4")):
                no_icd10 += 1
                rt.record_discrepancy(
                    reason="indication_without_icd10",
                    step="finalize_fda_molecules", artifact="fda",
                    record_key=(mol.get("clean_names") or [mid])[0],
                    detail={"kind": "indication_resolved_no_code",
                            "raw_text": ind.get("raw_text")})
        doc["_id"] = mid
        doc["molecule_id"] = mid
        doc["run_id"] = ctx.run_id
        doc["data_version"] = data_version
        doc["provenance"] = {
            "source_run_id": ctx.run_id,
            "source_collection": "StagingOpenFdaLabels_v_1",
            "built_at": built_at,
        }
        # Deterministic full-document write: the finalize is serial, so every
        # published write is single-writer; a re-run re-derives byte-identical
        # documents. Batched and UNORDERED so a document the validator refuses
        # leaves its conforming batch-mates written, and each refusal becomes
        # one schema_violation finding keyed by molecule.
        pending.append(_bson(doc))
        pending_keys.append(mid)
        if len(pending) >= 500:
            written += _insert_conforming(
                target_coll, pending, pending_keys,
                record_discrepancy=rt.record_discrepancy,
                validator=contract_validator)
            pending, pending_keys = [], []

    if pending:
        written += _insert_conforming(
            target_coll, pending, pending_keys,
            record_discrepancy=rt.record_discrepancy,
            validator=contract_validator)

    # The indexes this collection's queries need, created by the step that
    # creates the collection and after the fold, so the build never fights
    # index maintenance. Each is named with the query that needs it; there is
    # no unindexed predicate for anything to fall back on.
    built = []
    for keys, name in (
            # The external identity the children join on, and the immutable
            # one beside it.
            ([("rxcui_in", 1)], "rxcui_in_1"),
            ([("unii", 1)], "unii_1"),
            # Molecule identity as the record carries it, for the lookups
            # that start from a standard identifier rather than our key.
            ([("molecule.rxcui", 1)], "molecule_rxcui_1"),
            ([("molecule.unii", 1)], "molecule_unii_1"),
            # By name: the only way to find a molecule by what it is called,
            # and what an operator reaches for first.
            ([("molecule.clean_names", 1)], "molecule_clean_names_1"),
            # Indication to molecule: the diagnosis side of the product
            # question.
            ([("indications.icd10.level3", 1)], "indications_level3_1"),
            ([("indications.icd10.level4", 1)], "indications_level4_1"),
            # Which rung of the ladder answered, so the resolution rate is a
            # query rather than a scan.
            ([("identity_path", 1)], "identity_path_1")):
        built.append(target_coll.create_index(keys, name=name))
    _log.LogPipeline("INFO", "finalize_fda_molecules: indexes -> %s", built)

    folded = labels - orphans
    _log.LogPipeline(
        "INFO",
        "finalize_fda_molecules: labels=%d folded=%d orphans=%d molecules=%d "
        "rxnorm_joined=%d cleared=%d run_id=%s",
        labels, folded, orphans, written, rxnorm_joined, removed, ctx.run_id)
    return {"step": "finalize_fda_molecules",
            "labels": labels, "labels_folded": folded, "labels_orphaned": orphans,
            "molecules_written": written, "molecules_rxnorm_joined": rxnorm_joined,
            "molecules_with_identity": resolved,
            "molecules_without_identity": unresolved,
            "molecules_without_indication": no_indication,
            "indications_without_icd10": no_icd10,
            "indications_after_deterministic_collapse": collapsed,
            "indications_after_judged_grouping": judged,
            "cleared_prior_docs": removed}


def execute(ctx):
    return run_step(ctx)
