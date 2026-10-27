# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""Every query in the Medicare package is named here with the index that
serves it.

A collection of 26.4M records has no safe unindexed predicate, and a query
nobody noticed is how one gets added. So the test reads the source, finds
every Mongo query call, and requires each SHAPE to appear in the table below.
A new shape fails this test until it is entered with the index that serves it
-- which is the moment to create that index, not after a run has scanned.

The key is the query's shape, not its line number. A line number changes
whenever anything above it is edited, which makes the test fail for the wrong
reason and teaches its readers to re-stamp it without looking.

Values are an index name, or:
  BY_ID          the _id index, which every collection has
  SMALL_WHOLE    a whole read of a collection small enough to read whole
  CONTROL_PLANE  run records, work items, reservations -- thousands of rows,
                 indexed by the run lifecycle's own rules
"""

from __future__ import annotations

import ast
import pathlib

_PACKAGE = pathlib.Path(__file__).resolve().parents[2]

_QUERY_CALLS = frozenset((
    "find", "find_one", "count_documents", "distinct", "delete_many",
    "delete_one", "update_many", "update_one", "aggregate",
    "find_one_and_update", "replace_one",
))

SERVED_BY = {
    # the published ProviderMedicare collection, 26.4M
    "find(<computed>)": ("source_state_1__id_1 and molecule_id__id -- every "
                         "paged read is {partition key, _id: $gt}"),
    "update_many(source_state)": "source_state_1__id_1",
    "delete_many(_id)": "BY_ID",
    "distinct:npi": "npi_1",
    "distinct:source_state": "source_state_1__id_1",
    "distinct:molecule_id": "molecule_id__id on staging",
    "distinct:<computed>": ("prscrbr_state__id -- distinct on the leading "
                            "field of the staged state index"),

    # the Provider collection, 9.67M
    "find_one(npi)": "npi_1 on Provider_v_5",
    "find(npi)": "npi_1 on Provider_v_5",

    # the FDA molecule collection, 20,142
    "find_one(_id)": "BY_ID",
    "update_one(_id)": "BY_ID",

    # staged sources
    "count_documents(fda_record_number)": "fda_record_number_1",
    "find(fda_record_number)": "fda_record_number_1",
    "find(raw.TTY)": "tty on the staged RxNorm vocabulary",

    # whole reads of small collections
    "find()": ("SMALL_WHOLE -- the POS table (13,598), the transient "
               "(142,953) and the staged labels (262,900), each read once"),
    "count_documents()": "SMALL_WHOLE",
    "delete_many()": "SMALL_WHOLE -- drop the transient",
    "aggregate($unwind)": ("SMALL_WHOLE -- one pass over Provider for every "
                           "NUCC code's provider count"),

    # the control plane
    "update_one(run_id)": "CONTROL_PLANE",
    "find(run_id)": "CONTROL_PLANE",
    "count_documents(run_id)": "CONTROL_PLANE",
    "find_one(run_id,status)": "CONTROL_PLANE -- the first failed work item",
}


def _keys(node) -> list[str]:
    if not isinstance(node, ast.Dict):
        return ["<computed>"]
    out = []
    for key in node.keys:
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            out.append(key.value)
        else:
            out.append("<computed>")
    return out


def _shape(node: ast.Call) -> str:
    name = node.func.attr
    if not node.args:
        return f"{name}()"
    first = node.args[0]
    if name == "distinct":
        field = first.value if isinstance(first, ast.Constant) else "<computed>"
        return f"distinct:{field}"
    if name == "aggregate":
        stages = []
        if isinstance(first, ast.List):
            for element in first.elts:
                if isinstance(element, ast.Dict):
                    for key in element.keys:
                        if isinstance(key, ast.Constant):
                            stages.append(str(key.value))
        return f"aggregate({stages[0] if stages else ''})"
    return f"{name}({','.join(sorted(set(_keys(first))))})"


def _modules():
    yield from sorted(_PACKAGE.glob("*.py"))
    yield from sorted((_PACKAGE / "steps").glob("*.py"))


def _sites():
    for path in _modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in _QUERY_CALLS):
                yield path.name, node.lineno, _shape(node)


def test_every_query_shape_is_named_with_the_index_that_serves_it():
    unlisted = sorted({f"{shape}   at {name}:{line}"
                       for name, line, shape in _sites()
                       if shape not in SERVED_BY})
    assert not unlisted, (
        "these query shapes name no index; add each to SERVED_BY with the "
        "index that serves it, and create that index:\n  "
        + "\n  ".join(unlisted))


def test_the_table_names_no_shape_the_code_does_not_use():
    """The other direction, so the table cannot rot into a wish list."""
    used = {shape for _name, _line, shape in _sites()}
    stale = sorted(set(SERVED_BY) - used)
    assert not stale, f"the table names shapes no query uses: {stale}"


def test_the_published_collections_declare_the_indexes_the_table_names():
    from pipeline.MedicareProviderEvaluationDataPipeline.steps.clear_and_index_providermedicare_and_specialtymedicare import (
        INDEXES)
    names = {spec["name"] for spec in INDEXES["providermedicare"]}
    for needed in ("source_state_1__id_1", "molecule_id_1__id_1", "npi_1",
                   "rxcui_in_1", "unii_1", "nucc_codes_1", "billing_state_1"):
        assert needed in names, f"{needed} is not declared by the clear step"


def test_the_staging_collections_declare_their_indexes():
    from pipeline.MedicareProviderEvaluationDataPipeline.steps.medicare_load_staging import (
        STAGING_INDEXES)
    declared = {src: {s["name"] for s in specs}
                for src, specs in STAGING_INDEXES.items()}
    assert "prscrbr_state__id" in declared["medicare_partd"]
    assert "molecule_id__id" in declared["medicare_partd"]
    assert "tty" in declared["rxnorm"]
    assert "rela" in declared["rxnorm_rel"]
    assert "fda_record_number_1" in declared["openfda_labels"]


def test_no_staging_index_is_declared_without_a_query_that_names_it():
    """The audit that removed prscrbr_npi, kept as a test."""
    from pipeline.MedicareProviderEvaluationDataPipeline.steps.medicare_load_staging import (
        STAGING_INDEXES)
    source = "\n".join(p.read_text(encoding="utf-8") for p in _modules())
    for specs in STAGING_INDEXES.values():
        for spec in specs:
            leading = spec["keys"][0][0]
            field = leading.split("raw.", 1)[-1]
            assert field in source or leading in source, (
                f"index {spec['name']} leads with {leading}, which no query "
                f"in the package names")
