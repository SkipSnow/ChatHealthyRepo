"""EPIC-008-F-012-S-002-REQ-B-006 - promotions are auditably approved by a human.

    Every code promotion MUST be approved by a human operator prior to any
    changes in the code REPO. This approval MUST be absolutely auditable,
    such that there is proof that a human and not an agent did the approval.

The negative cases carry the requirement. A promote that runs with no
approval, with someone else's approval, with an approval a program could have
produced, or with an approval that reached no durable record, must leave the
destination ref exactly where it was.

The operator's answer is faked at the boundary - request_authorization is
replaced - because these tests are about what the gate does with an answer,
not about whether a browser reports a real click. The proof fields are set
explicitly so both the honest and the forged shapes can be exercised.
"""
from __future__ import annotations

import pathlib
import sys

import pytest

CODE = pathlib.Path(__file__).resolve().parents[1] / "code"
if str(CODE) not in sys.path:
    sys.path.insert(0, str(CODE))

import promote_authorization_worker as paw  # noqa: E402
import authorization_record  # noqa: E402  (paw resolves the library first)
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

REQ = "EPIC-008-F-012-S-002-REQ-B-006"


class FakeDecision:
    """What request_authorization returns, with the click evidence set by us."""

    def __init__(self, verdict="approve", human_click=True, travel=214,
                 seconds=12):
        self.verdict = verdict
        self.human_click = human_click
        self.pointer_travel_px = travel
        self.seconds_waited = seconds

    @property
    def approved(self):
        return self.verdict == "approve"


class FakeCollection:
    """Stands in for DevOpsAdmin.Authorizations."""

    def __init__(self, fail_on_insert=False):
        self.documents = []
        self.updates = []
        self.fail_on_insert = fail_on_insert

    def insert_one(self, document):
        if self.fail_on_insert:
            raise ChatHealthyException("cluster_unreachable",
                                       "cluster unreachable")
        self.documents.append(document)

        class Result:
            inserted_id = len(self.documents)
        return Result()

    def update_one(self, query, update):
        self.updates.append((query, update))


def facts(source="dev", destination="qa"):
    return paw.PromotionFacts(
        source_environment=source, destination_environment=destination,
        source_branch=source, destination_branch=destination,
        commit="29d19a48aaaabbbb", commit_subject="a change worth advancing",
        commits_ahead=7)


@pytest.fixture
def gate(monkeypatch):
    """A worker whose collection is a fake and whose operator answer is ours."""

    def build(decision=None, fail_on_insert=False, promotion=None):
        worker = paw.PromoteAuthorizationWorker(promotion or facts(),
                                                operator="skip")
        collection = FakeCollection(fail_on_insert=fail_on_insert)
        # The gate no longer owns a connection. There is one writer for every
        # authorization -- commit, promotion, migration -- so the fake stands
        # in for that writer's collection rather than for a method on this
        # worker.
        monkeypatch.setattr(authorization_record, "collection",
                            lambda: collection)
        monkeypatch.setattr(
            paw, "request_authorization",
            lambda *a, **k: decision if decision is not None else FakeDecision())
        return worker, collection
    return build


class TestPromoteIsRefused:
    def test_operator_rejected(self, gate):
        """REJECT MUST leave the destination ref where it was."""
        worker, collection = gate(FakeDecision(verdict="reject"))
        assert worker.authorize() is None
        assert collection.documents[0]["verdict"] == "reject"

    def test_operator_never_answered(self, gate):
        """A timed-out prompt is not an approval."""
        worker, _ = gate(FakeDecision(verdict="timeout"))
        assert worker.authorize() is None

    def test_promotion_is_not_between_adjacent_environments(self, gate):
        """dev to prod skips a step and is refused before anyone is asked."""
        worker, collection = gate(promotion=facts("dev", "prod"))
        with pytest.raises(ChatHealthyException) as caught:
            worker.authorize()
        assert caught.value.mode == "promotion_not_adjacent"
        assert collection.documents == []

    def test_the_record_names_this_promotion_and_no_other(self, gate):
        """One record authorizes one promotion: the environment pair and the
        commit are both on it, so it cannot be read as authorizing another."""
        worker, collection = gate()
        worker.authorize()
        written = collection.documents[0]
        assert written["source_environment"] == "dev"
        assert written["destination_environment"] == "qa"
        assert written["commit"] == "29d19a48aaaabbbb"

    def test_a_promotion_record_is_not_a_commit_record(self, gate):
        """authorization_type is what stops a commit authorization satisfying
        a promotion, since both live in the same collection."""
        worker, collection = gate()
        worker.authorize()
        assert collection.documents[0]["authorization_type"] == "promotion"


class TestApprovalMustBeHuman:
    def test_approval_without_a_trusted_press_is_refused(self, gate):
        """An approval a program could have produced is not an approval."""
        worker, collection = gate(FakeDecision(human_click=False))
        assert worker.authorize() is None
        assert collection.documents[0]["outcome"] == "refused_no_human_presence"

    def test_the_record_carries_the_human_evidence(self, gate):
        """A later reader establishes that a human answered without having to
        trust that the gate behaved."""
        worker, collection = gate(FakeDecision(travel=214))
        worker.authorize()
        proof = collection.documents[0]["proof"]
        assert proof["is_trusted"] is True
        assert proof["press_observed"] is True
        assert proof["pointer_travel_px"] == 214


class TestApprovalMustBeAuditable:
    def test_record_cannot_be_written_so_promote_does_not_proceed(self, gate):
        """Unrecordable is unauthorized."""
        worker, _ = gate(fail_on_insert=True)
        with pytest.raises(ChatHealthyException) as caught:
            worker.authorize()
        assert caught.value.mode == "authorization_unrecordable"

    def test_the_record_is_written_before_the_caller_may_move_the_ref(self, gate):
        """'Prior to any changes in the code REPO' is ordering, and it is
        observable: authorize() does not return until the record exists."""
        worker, collection = gate()
        assert collection.documents == []
        record_id = worker.authorize()
        assert record_id is not None
        assert len(collection.documents) == 1

    def test_the_record_lives_outside_the_repository_being_promoted(self):
        """A file inside the governed tree is not evidence."""
        assert authorization_record.AUTHORIZATION_DATABASE == "DevOpsAdmin"
        assert authorization_record.AUTHORIZATION_COLLECTION == "Authorizations"

    def test_a_failed_promotion_records_its_outcome(self, gate):
        """Without this the record says a promotion was authorized while the
        ref never moved, and the record is the only evidence."""
        worker, collection = gate()
        record_id = worker.authorize()
        worker.record_outcome(record_id, "failed_ref_did_not_move")
        outcome = collection.documents[1]
        assert outcome["authorization_type"] == "promotion_outcome"
        assert outcome["authorization_id"] == record_id
        assert outcome["outcome"] == "failed_ref_did_not_move"

    def test_the_authorization_record_is_never_altered(self, gate):
        """The outcome is appended, not written over the authorization.

        DevOpsUser is granted INSERT and no UPDATE on this collection, for the
        same reason role_data_migrator holds none: a governance record that can
        be altered is not evidence.
        """
        worker, collection = gate()
        record_id = worker.authorize()
        authorization = dict(collection.documents[0])
        worker.record_outcome(record_id, "promoted")
        assert collection.updates == []
        assert collection.documents[0] == authorization
        assert collection.documents[1]["outcome"] == "promoted"

class TestPromoteIsAllowed:
    def test_approved_promotion_is_authorized_and_recorded(self, gate):
        """The only path that lets the ref move."""
        worker, collection = gate()
        record_id = worker.authorize()
        assert record_id is not None
        written = collection.documents[0]
        assert written["verdict"] == "approve"
        assert written["operator"] == "skip"
        assert written["outcome"] == "pending"
