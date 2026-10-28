# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""The findings contract: the schema, the JSON, and the code that reads it.

Three things have to agree and until now nothing checked that they did.
pipeline_config.json carried discrepancy_abort that no code read, a per-class
max that no code read, and a per-class report_display_cap the schema did not
declare -- and the config validated anyway, because the schema's finding_types
object declared no properties at all.

So this file tests the agreement in both directions:

  * every environment's config validates against the published contract
  * the contract REFUSES the shapes it is supposed to refuse (negative cases),
    because a schema that accepts everything is the defect that let the above
    happen
  * the code that grades a finding reads the config the way the config is
    written (positive), and reports an unknown class rather than inventing a
    grade for it (negative)
"""

from __future__ import annotations

import copy
import json
import pathlib

import jsonschema
import pytest

_ROOT = pathlib.Path(__file__).resolve().parents[4]
_CONFIG = _ROOT / "brain" / "machine_artifacts" / "content" / "pipeline_config.json"
_SCHEMA = _ROOT / "Website" / "schemas" / "ChatHealthyPipelineConfigSchema.json"

_REQUIRED_PER_CLASS = ("severity", "fatal_at_count", "max")
_SEVERITIES = ("warning", "error", "fatal")


def _config() -> dict:
    return json.loads(_CONFIG.read_text(encoding="utf-8"))


def _schema() -> dict:
    return json.loads(_SCHEMA.read_text(encoding="utf-8"))


def _class_schema(schema: dict) -> dict:
    return (schema["$defs"]["EnvConfig"]["properties"]["discrepancy_report"]
            ["properties"]["finding_types"]["additionalProperties"])


def _envs(config: dict):
    for entry in config["pipeline_config"]:
        yield entry["env"], entry


# ── the JSON validates against the schema ────────────────────────────────────
def test_the_config_validates_against_its_published_contract():
    jsonschema.validate(_config(), _schema())


def test_every_environment_declares_the_same_finding_classes():
    """A class graded in dev and absent in prod is a run that grades a finding
    one way on the way up and another way at the top."""
    per_env = {env: set((entry["discrepancy_report"]["finding_types"]).keys())
               for env, entry in _envs(_config())}
    envs = sorted(per_env)
    first = per_env[envs[0]]
    for env in envs[1:]:
        assert per_env[env] == first, (
            f"{env} declares a different class set than {envs[0]}: "
            f"only in {env}: {sorted(per_env[env] - first)}; "
            f"only in {envs[0]}: {sorted(first - per_env[env])}")


def test_every_class_carries_the_three_required_facts():
    for env, entry in _envs(_config()):
        for name, cls in entry["discrepancy_report"]["finding_types"].items():
            for field in _REQUIRED_PER_CLASS:
                assert field in cls, f"{env}/{name} has no {field!r}"
            assert cls["severity"] in _SEVERITIES, (
                f"{env}/{name} severity {cls['severity']!r} is not one of "
                f"{_SEVERITIES}")


def test_every_environment_declares_a_per_run_findings_ceiling():
    for env, entry in _envs(_config()):
        thresholds = entry.get("failure_thresholds") or {}
        ceiling = thresholds.get("discrepancy_abort")
        assert isinstance(ceiling, int) and ceiling >= 1, (
            f"{env} declares no usable failure_thresholds.discrepancy_abort: "
            f"{ceiling!r}")


# ── the schema refuses what it must refuse ───────────────────────────────────
def _validate_class(cls_doc: dict) -> None:
    jsonschema.validate(cls_doc, _class_schema(_schema()))


def test_a_class_may_declare_the_documented_fields():
    _validate_class({"severity": "error", "fatal_at_count": 5000, "max": None})
    _validate_class({"severity": "fatal", "fatal_at_count": None, "max": None})
    _validate_class({"severity": "warning", "fatal_at_count": None,
                     "max": 25, "report_display_cap": 25})


@pytest.mark.parametrize("bad, why", [
    ({"fatal_at_count": None, "max": None}, "no severity"),
    ({"severity": "error", "max": None}, "no fatal_at_count"),
    ({"severity": "error", "fatal_at_count": None}, "no max"),
    ({"severity": "critical", "fatal_at_count": None, "max": None},
     "severity outside the three grades"),
    ({"severity": "error", "fatal_at_count": 0, "max": None},
     "a ceiling of zero aborts before anything happens"),
    ({"severity": "error", "fatal_at_count": "5000", "max": None},
     "a ceiling as a string"),
    ({"severity": "error", "fatal_at_count": None, "max": None,
      "fatal_at_cuont": 5000}, "a misspelled field"),
])
def test_the_contract_refuses_a_malformed_class(bad, why):
    with pytest.raises(jsonschema.ValidationError):
        _validate_class(bad)
    assert why  # the reason travels with the case


def test_the_contract_refuses_a_run_ceiling_of_zero():
    config = _config()
    config["pipeline_config"][0]["failure_thresholds"]["discrepancy_abort"] = 0
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(config, _schema())


def test_the_contract_refuses_a_config_with_no_run_ceiling():
    config = _config()
    del config["pipeline_config"][0]["failure_thresholds"]["discrepancy_abort"]
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(config, _schema())


# ── the code reads the config the way the config is written ──────────────────
def _severity_for():
    from pipeline.run_lifecycle.pipeline_runtime import _severity_for
    return _severity_for


def test_the_grader_returns_the_severity_the_config_declares():
    grade = _severity_for()
    for env, entry in _envs(_config()):
        block = entry["discrepancy_report"]
        for name, cls in block["finding_types"].items():
            assert grade(block, name) == cls["severity"], (
                f"{env}/{name}: the grader disagrees with the config")


def test_the_grader_refuses_a_class_the_config_does_not_name():
    """An unnamed class is a configuration defect, surfaced rather than
    silently graded (LLD I.5.5 field contract)."""
    from chathealthy_lib.exceptions import ChatHealthyException
    grade = _severity_for()
    block = _config()["pipeline_config"][0]["discrepancy_report"]
    with pytest.raises(ChatHealthyException):
        grade(block, "a_class_no_pipeline_declares")


def test_schema_violation_is_fatal_in_every_environment():
    """Operator ruling 2026-10-28. A record the store's validator refuses
    means the code and the contract disagree, which is a defect rather than
    bad data, so it aborts on the first occurrence."""
    for env, entry in _envs(_config()):
        cls = entry["discrepancy_report"]["finding_types"]["schema_violation"]
        assert cls["severity"] == "fatal", f"{env} grades it {cls['severity']}"


@pytest.mark.parametrize("name", [
    "drug_without_rxnorm_molecule",
    "molecule_without_indication",
    "indication_without_icd10",
])
def test_the_three_medicare_classes_escalate_at_five_thousand(name):
    """Operator ruling 2026-10-28."""
    for env, entry in _envs(_config()):
        cls = entry["discrepancy_report"]["finding_types"][name]
        assert cls["fatal_at_count"] == 5000, (
            f"{env}/{name} escalates at {cls['fatal_at_count']}")


def test_every_class_the_medicare_code_emits_is_declared():
    """A class the code can emit and the config does not name would be graded
    by nothing. Read from the source rather than listed here, so a new finding
    added to a step fails this test until it is declared."""
    package = _ROOT / "pipeline" / "MedicareProviderEvaluationDataPipeline"
    marker = 'reason="'
    emitted: set[str] = set()
    for path in package.rglob("*.py"):
        if "__pycache__" in str(path) or "tests" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        at = text.find(marker)
        while at != -1:
            start = at + len(marker)
            end = text.find('"', start)
            if end != -1:
                name = text[start:end]
                if name and name.replace("_", "").isalnum():
                    emitted.add(name)
            at = text.find(marker, start)
    assert emitted, "no finding classes found in the Medicare package"
    declared = set(_config()["pipeline_config"][0]
                   ["discrepancy_report"]["finding_types"])
    undeclared = sorted(emitted - declared)
    assert not undeclared, (
        "the Medicare code emits finding classes the config does not declare, "
        f"so nothing grades them: {undeclared}")
