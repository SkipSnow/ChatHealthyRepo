"""Measure BRAND-key and MOLECULE-key coverage over the COMPLETE openFDA label corpus.

No sampling, no api.fda.gov limit/skip paging. Streams every partition the
pipeline's own fetch_spec names (pipeline_config.json source_name
"openfda_labels": mode sharded_manifest, manifest https://api.fda.gov/download.json,
list_path results.drug.label.partitions, url_field file) and runs the pipeline's
OWN committed functions over every record:

  _has_indication  / _otc_basis   -- steps/index_fda_prescription_records.py
  _shape_fda_record               -- steps/build_fda_label_resolution.py
  brand_join_key / molecule_id    -- medicare_normalization.py

In scope = states an indication AND is not OTC, exactly as the pipeline decides it.
OTC records are excluded from every in-scope number.

Streamed with ijson and discarded per record; each partition is downloaded to a
temp file, read, and deleted. Nothing is written outside _oneshots.
"""
import json
import os
import sys
import urllib.request
import zipfile
from collections import Counter

sys.path.insert(0, r"C:\CHATHealthyLLC")
sys.path.insert(0, r"C:\CHATHealthyLLC\ChatHealthyLib\src")
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

_log = ChatHealthyLoggingService()

import ijson  # noqa: E402

from pipeline.MedicareProviderEvaluationDataPipeline.steps.index_fda_prescription_records import (  # noqa: E402
    _has_indication, _otc_basis)
from pipeline.MedicareProviderEvaluationDataPipeline.steps.build_fda_label_resolution import (  # noqa: E402
    _shape_fda_record)
from pipeline.MedicareProviderEvaluationDataPipeline.medicare_normalization import (  # noqa: E402
    brand_join_key, molecule_id)

OUT = r"C:\CHATHealthyLLC\_oneshots\fda_corpus_result.json"
PROG = r"C:\CHATHealthyLLC\_oneshots\fda_corpus_progress.txt"
TMP = r"C:\CHATHealthyLLC\_oneshots\_fda_part.zip"

counts = Counter()
no_brand_fields = Counter()
no_molecule_fields = Counter()
no_either_fields = Counter()

# openfda sub-fields that could identify a drug, and label-level narrative
# fields that name a substance in prose.
OPENFDA_ID_FIELDS = (
    "brand_name", "generic_name", "substance_name", "rxcui", "unii",
    "product_ndc", "package_ndc", "spl_id", "spl_set_id",
    "application_number", "manufacturer_name", "nui", "upc",
    "pharm_class_epc", "pharm_class_moa", "pharm_class_cs", "pharm_class_pe",
    "product_type", "route",
)
LABEL_ID_FIELDS = (
    "active_ingredient", "spl_product_data_elements", "description",
    "package_label_principal_display_panel", "spl_unclassified_section",
    "how_supplied", "clinical_pharmacology", "dosage_and_administration",
    "openfda",
)


def note(msg):
    with open(PROG, "a", encoding="utf-8") as fh:
        fh.write(msg + "\n")
    _log.info("%s", msg)


def _text(value) -> str:
    if isinstance(value, list):
        return " ".join(str(v) for v in value).strip()
    if isinstance(value, dict):
        return "1" if value else ""
    return str(value or "").strip()


def _first_nonempty(values) -> str:
    if isinstance(values, str):
        values = [values]
    for v in values or []:
        text = str(v or "").strip()
        if text:
            return text
    return ""


def _profile(label, openfda, sink):
    """Record, field by field, what this label DOES carry that identifies a drug."""
    if not openfda:
        sink["openfda_object_absent_or_empty"] += 1
    for field in OPENFDA_ID_FIELDS:
        if _first_nonempty(openfda.get(field)):
            sink["openfda." + field] += 1
    for field in LABEL_ID_FIELDS:
        if _text(label.get(field)):
            sink["label." + field] += 1


def classify(label):
    counts["corpus_total"] += 1

    # --- the pipeline's own in-scope decision, in the pipeline's own order ---
    if not _has_indication(label):
        counts["excluded_no_indication"] += 1
        return
    basis = _otc_basis(label)
    if basis is not None:
        counts["excluded_otc"] += 1
        counts["otc_basis:" + basis.split(":")[0]] += 1
        return
    counts["in_scope_rx"] += 1

    openfda = label.get("openfda") or {}

    # --- the BRAND key: openfda.brand_name through brand_join_key ---
    brand_raw = _first_nonempty(openfda.get("brand_name"))
    brand_key = brand_join_key(brand_raw) if brand_raw else ""
    has_brand = bool(brand_key)
    if has_brand:
        counts["brand_key_present"] += 1
    else:
        counts["brand_key_absent"] += 1
        if brand_raw:
            counts["brand_raw_present_but_key_empty"] += 1

    # --- the MOLECULE key: the pipeline's own cascade, then molecule_id ---
    shaped = _shape_fda_record(label)
    source = shaped.get("_molecule_source")
    molecule = str(shaped.get("molecule") or "").strip()
    counts["molecule_source:" + str(source)] += 1
    has_molecule = bool(molecule) and molecule_id(molecule) is not None
    if has_molecule:
        counts["molecule_key_present"] += 1
    else:
        counts["molecule_key_absent"] += 1
        if molecule:
            counts["molecule_name_present_but_id_null"] += 1

    # the raw harmonized molecule fields, measured independently of the cascade
    if _first_nonempty(openfda.get("substance_name")):
        counts["openfda_substance_name_present"] += 1
    if _first_nonempty(openfda.get("generic_name")):
        counts["openfda_generic_name_present"] += 1
    if _text(label.get("active_ingredient")):
        counts["label_active_ingredient_present"] += 1

    # --- cross-tab, so the two keys can be compared directly ---
    counts["xtab:brand=%s,molecule=%s" % (int(has_brand), int(has_molecule))] += 1

    if not has_brand:
        _profile(label, openfda, no_brand_fields)
    if not has_molecule:
        _profile(label, openfda, no_molecule_fields)
    if not has_brand and not has_molecule:
        _profile(label, openfda, no_either_fields)


def main():
    for path in (OUT, PROG):
        if os.path.exists(path):
            os.remove(path)
    manifest = json.loads(
        urllib.request.urlopen("https://api.fda.gov/download.json", timeout=180).read())
    lab = manifest["results"]["drug"]["label"]
    parts = lab["partitions"]
    note("export=%s declared_records=%s partitions=%d" % (
        manifest.get("meta", {}).get("last_updated"),
        lab.get("total_records"), len(parts)))

    for i, part in enumerate(parts, 1):
        url = part["file"]
        note("[%d/%d] downloading %s (%s MB, declared %s records)" % (
            i, len(parts), url.rsplit("/", 1)[-1], part.get("size_mb"),
            part.get("records")))
        with urllib.request.urlopen(url, timeout=1800) as src, open(TMP, "wb") as dst:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    break
                dst.write(chunk)
        before = counts["corpus_total"]
        with zipfile.ZipFile(TMP) as zf:
            for name in zf.namelist():
                with zf.open(name) as member:
                    for label in ijson.items(member, "results.item",
                                             use_float=True):
                        classify(label)
        os.remove(TMP)
        note("[%d/%d] read=%d cumulative total=%d in_scope_rx=%d "
             "brand=%d molecule=%d" % (
                 i, len(parts), counts["corpus_total"] - before,
                 counts["corpus_total"], counts["in_scope_rx"],
                 counts["brand_key_present"], counts["molecule_key_present"]))

    result = {
        "export_date": manifest.get("meta", {}).get("last_updated"),
        "declared_total_records": lab.get("total_records"),
        "partitions_read": len(parts),
        "counts": dict(counts),
        "no_brand_key_population_fields": dict(no_brand_fields),
        "no_molecule_key_population_fields": dict(no_molecule_fields),
        "no_either_key_population_fields": dict(no_either_fields),
    }
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1, default=str)
    note("DONE -> " + OUT)
    note(json.dumps(dict(counts), indent=1, default=str))


main()
