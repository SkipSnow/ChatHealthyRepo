# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).
"""Deploy argument guarding + truthful approval/record.

The approval screen and the authorization record must name the REAL target_ids
and packages and build being deployed -- not the group word ('cloudflare') that
was typed, which resolved to an empty package list and build 0. And a bad
argument (no target, a target that does not exist, a package on no target, a
package not declared on the named target) must abend before authorization and
be recorded to the authorization collection, not die as a bare traceback.
"""
from __future__ import annotations

import pathlib
import sys
import types

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[2]
for _p in (_REPO / "ChatHealthyLib" / "src",
           _REPO / "architecture" / "DevOpsBuildDeployAndEnvironmentManagement",
           _REPO / "architecture" / "EngineeringRuleEnforcement" / "code"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import deploy_chathealthy as dc
import deploy_authorization_worker as daw
from chathealthy_lib.exceptions import ChatHealthyException


class _FakeApproval:
    verdict = "approve"
    human_click = True
    approved = True
    seconds_waited = 0
    message = ""


@pytest.fixture
def recorded(monkeypatch):
    """Capture every authorization document; no screen pops, no DB write."""
    docs: list[dict] = []
    monkeypatch.setattr(daw, "request_authorization",
                        lambda *a, **k: _FakeApproval())
    monkeypatch.setattr(daw.authorization_record, "append",
                        lambda doc, tolerate_failure=False: docs.append(doc) or "recid")
    # Fix the build identity so the test does not depend on build/ output.
    monkeypatch.setattr(dc, "_build_identity",
                        lambda repo_root, targets, packages: (2323, "deadbeef"))
    return docs


def _args(target, package=""):
    return types.SimpleNamespace(env="dev", target=target, package=package, tests="")


def test_group_form_names_real_target_packages_build(recorded):
    worker, rec_id = dc._authorize_deployment(
        _REPO, _args("cloudflare", "json_schemas,runtime_driver,static_pages,Data"))
    assert rec_id == "recid"
    doc = recorded[-1]
    assert "target_cloudflare_pages_website" in doc["targets"]
    assert "cloudflare" not in doc["targets"]          # the group word is gone
    assert doc["build_number"] == 2323                 # not 0
    pkgs = doc["packages"]["target_cloudflare_pages_website"]
    assert set(pkgs) == {"json_schemas", "runtime_driver", "static_pages", "Data"}
    assert "(none)" not in doc["deploying"]            # names the real packages


def test_nonexistent_target_abends_and_is_recorded(recorded):
    with pytest.raises(ChatHealthyException):
        dc._authorize_deployment(_REPO, _args("target_that_does_not_exist"))
    doc = recorded[-1]
    assert doc["verdict"] == "aborted_bad_argument"
    assert doc["reason"]


def test_package_that_exists_nowhere_abends_and_is_recorded(recorded):
    with pytest.raises(ChatHealthyException):
        dc._authorize_deployment(_REPO, _args("cloudflare", "no_such_package"))
    doc = recorded[-1]
    assert doc["verdict"] == "aborted_bad_argument"
    assert "no_such_package" in doc["reason"]


def test_no_target_abends_and_is_recorded(recorded):
    with pytest.raises(ChatHealthyException):
        dc._authorize_deployment(_REPO, _args(""))
    doc = recorded[-1]
    assert doc["verdict"] == "aborted_bad_argument"
    assert doc["reason"]
