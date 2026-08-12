"""Rule-065-ENF-005 — identity and role-grant changes require approval.

This enforcement runs on every commit and had no test. It decides whether
the operator is asked before a change to who can reach what, so a hole in it
means an identity or a role grant can land unseen.

The prompt itself is not exercised here — it blocks on a human. What is
proved is the decision that precedes it: given a staged deployment
architecture, does the worker correctly detect that the identity surface
changed, and correctly stay silent when it did not. A worker that always
detects nothing would never prompt, and would pass every test that only
checked it ran.
"""
from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

import identity_approval_worker as iaw

BASE = {
    "IdentityCatalog": [
        {"identity_id": "pipelineEditor", "kind": "managed_identity"},
        {"identity_id": "frontendUser", "kind": "managed_identity"},
    ],
    "CustomRoleCatalog": [
        {"role_id": "pipelineEditorRole", "actions": ["find", "insert"]},
    ],
    "DeploymentTargetRecord": [
        {"target_id": "target_a", "allowed_roles": ["Key Vault Secrets User"]},
    ],
}


def _worker(head: dict, staged: dict):
    w = iaw.IdentityApprovalWorker("Rule-065-ENF-005")
    w._load_head_manifest = lambda: copy.deepcopy(head)       # type: ignore[assignment]
    w._load_staged_manifest = lambda: copy.deepcopy(staged)   # type: ignore[assignment]
    return w


def test_no_identity_change_produces_no_summary():
    """The silent case must be genuinely silent, or every commit prompts."""
    w = _worker(BASE, copy.deepcopy(BASE))
    assert w._compute_identity_delta_summary() is None


def test_unrelated_change_produces_no_summary():
    staged = copy.deepcopy(BASE)
    staged["DeploymentTargetRecord"][0]["files"] = ["something.py"]
    w = _worker(BASE, staged)
    assert w._compute_identity_delta_summary() is None, \
        "a non-identity edit must not trigger the identity gate"


def test_added_identity_is_detected():
    staged = copy.deepcopy(BASE)
    staged["IdentityCatalog"].append({"identity_id": "DevOpsUser", "kind": "managed_identity"})
    summary = _worker(BASE, staged)._compute_identity_delta_summary()
    assert summary is not None, "an added identity must be detected"
    assert "DevOpsUser" in summary and "added" in summary


def test_removed_identity_is_detected():
    staged = copy.deepcopy(BASE)
    staged["IdentityCatalog"] = [i for i in staged["IdentityCatalog"]
                                 if i["identity_id"] != "frontendUser"]
    summary = _worker(BASE, staged)._compute_identity_delta_summary()
    assert summary is not None, "a removed identity must be detected"
    assert "frontendUser" in summary and "removed" in summary


def test_modified_identity_is_detected():
    staged = copy.deepcopy(BASE)
    staged["IdentityCatalog"][0]["kind"] = "service_principal"
    summary = _worker(BASE, staged)._compute_identity_delta_summary()
    assert summary is not None, "a changed identity field must be detected"
    assert "pipelineEditor" in summary and "modified" in summary


def test_custom_role_change_is_detected():
    staged = copy.deepcopy(BASE)
    staged["CustomRoleCatalog"][0]["actions"].append("remove")
    summary = _worker(BASE, staged)._compute_identity_delta_summary()
    assert summary is not None, "a role's actions changing must be detected"
    assert "pipelineEditorRole" in summary


def test_allowed_roles_change_is_detected():
    staged = copy.deepcopy(BASE)
    staged["DeploymentTargetRecord"][0]["allowed_roles"].append("Contributor")
    summary = _worker(BASE, staged)._compute_identity_delta_summary()
    assert summary is not None, "a per-target grant change must be detected"
    assert "target_a" in summary


def test_absent_staged_manifest_produces_no_summary():
    """A commit that does not touch the file must not prompt."""
    w = _worker(BASE, None)
    assert w._compute_identity_delta_summary() is None
