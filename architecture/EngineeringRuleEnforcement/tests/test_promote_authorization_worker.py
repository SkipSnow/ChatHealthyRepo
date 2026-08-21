"""EPIC-008-F-012-S-002-REQ-B-006 - promotions are auditably approved by a human.

    Every code promotion MUST be approved by a human operator prior to any
    changes in the code REPO. This approval MUST be absolutely auditable,
    such that there is proof that a human and not an agent did the approval.

STUB. The gate does not exist yet, so every case below fails. A skip would
report green for a requirement nothing implements; these stay red until the
worker is written, and each one goes green only when the thing it names is
true.

The negative cases carry the requirement. A promote that runs with no
approval, with someone else's approval, with an approval a program could have
produced, or with an approval that reached no durable record, is the failure
this requirement exists to prevent -- and each of those must leave the
destination ref exactly where it was.
"""
from __future__ import annotations

import pytest

REQ = "EPIC-008-F-012-S-002-REQ-B-006"

try:
    from architecture.EngineeringRuleEnforcement.code import (  # type: ignore
        promote_authorization_worker as worker,
    )
except ImportError:
    worker = None


def _gate():
    if worker is None:
        pytest.fail(f"{REQ}: no promote authorization gate exists")
    return worker


class TestPromoteIsRefused:
    """No approval, or not this promotion's approval -> the ref does not move."""

    def test_no_authorization_at_all(self):
        """A promote fired with nothing authorizing it MUST NOT change the repo."""
        _gate()

    def test_operator_rejected(self):
        """REJECT MUST leave the destination ref where it was."""
        _gate()

    def test_operator_never_answered(self):
        """A timed-out prompt is not an approval."""
        _gate()

    def test_authorization_names_a_different_promotion(self):
        """An approval for dev->qa MUST NOT authorize qa->prod, and an
        approval of commit A MUST NOT authorize the promotion of commit B."""
        _gate()

    def test_authorization_already_spent(self):
        """A prior promotion's record MUST NOT authorize a second one."""
        _gate()

    def test_authorization_is_a_commit_authorization(self):
        """A commit authorization MUST NOT satisfy a promotion. Different
        fact, different type."""
        _gate()


class TestApprovalMustBeHuman:
    """Proof that a human, not an agent, approved."""

    def test_click_without_mouse_movement(self):
        """A click with no preceding pointer travel is a program. Refused."""
        _gate()

    def test_synthetic_click(self):
        """An untrusted event -- dispatched rather than made by a person --
        is refused."""
        _gate()

    def test_record_carries_the_human_evidence(self):
        """The stored record MUST carry the movement and press evidence, so a
        later reader can tell a person answered without trusting the gate."""
        _gate()


class TestApprovalMustBeAuditable:
    """The record is a condition of the promotion, not a by-product."""

    def test_record_cannot_be_written_so_promote_does_not_proceed(self):
        """Cluster unreachable at record time MUST abort the promotion."""
        _gate()

    def test_record_is_written_before_the_repo_changes(self):
        """'Prior to any changes in the code REPO' is ordering, and it is
        testable: the record exists at the moment the ref still points at the
        old commit."""
        _gate()

    def test_record_is_outside_the_repository_being_promoted(self):
        """A file inside the tree the rule governs is not evidence. The
        record lives where the promoting identity can append and not edit."""
        _gate()

    def test_record_identifies_the_promotion(self):
        """Source env, destination env, commit, branch, operator, time --
        enough that the record names one promotion and no other."""
        _gate()


class TestPromoteIsAllowed:
    def test_approved_promotion_proceeds_and_is_recorded(self):
        """The only path that moves the ref: a human approved this exact
        promotion, and the record proving it exists first."""
        _gate()
