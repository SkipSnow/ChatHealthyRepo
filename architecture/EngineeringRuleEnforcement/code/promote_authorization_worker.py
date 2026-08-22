"""Rule-065-ENF-010 — a promotion is approved by a human, provably.

EPIC-008-F-012-S-002-REQ-B-006: every code promotion is approved by a human
operator prior to any changes in the code REPO, and that approval is
absolutely auditable, such that there is proof a human and not an agent gave
it.

Design V31 section 4.14. A promotion is a different fact from a commit: a
commit asks whether a change may enter, and is judged on content; a promotion
asks whether a baseline already on dev may advance, and authors nothing. The
two therefore carry different record types, and a commit authorization can
never satisfy a promotion.

The ordering in this module is the requirement. The record is written while
the destination ref still points at the old commit, and the caller does not
move the ref until authorize() returns an Authorization. A record that cannot
be written refuses the promotion: an authorization that left no record did not
occur.
"""
from __future__ import annotations

import sys as _sys
import pathlib as _pl

for _d in _pl.Path(__file__).resolve().parents:
    if (_d / ".git").exists():
        _lib = _d / "ChatHealthyLib" / "src"
        if str(_lib) not in _sys.path:
            _sys.path.insert(0, str(_lib))
        break

import os as _ch_os

_ch_os.environ.setdefault("CH_LOG_DESTINATION", "stderr")

from dataclasses import dataclass, asdict, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.human_authorization import (  # noqa: E402
    request_authorization)
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

_log = ChatHealthyLoggingService()

AUTHORIZATION_TYPE = "promotion"
AUTHORIZATION_DATABASE = "GovernanceAdminDb"
AUTHORIZATION_COLLECTION = "Authorizations"
AUTHORIZATION_IDENTITY = "DevOpsUser"
AUTHORIZATION_CLUSTER = "ChatHealthyFrontEnd"
TIMEOUT_SECONDS = 600
ADJACENT_PAIRS = (("local", "dev"), ("dev", "qa"), ("qa", "prod"))


@dataclass(frozen=True)
class PromotionFacts:
    """The identity of one promotion.

    These facts reach the operator on the page and the record in the same
    shape, so the thing approved and the thing recorded cannot differ.
    """

    source_environment: str
    destination_environment: str
    source_branch: str
    destination_branch: str
    commit: str
    commit_subject: str
    commits_ahead: int

    def subject(self) -> str:
        return (f"{self.source_environment} to {self.destination_environment}: "
                f"{self.commit[:12]} {self.commit_subject}")


@dataclass
class HumanPresenceProof:
    """Evidence that a person, not a program, answered.

    Carried into the record so a later reader establishes it without trusting
    that the gate behaved. Evidence that exists only at the moment of the
    click proves nothing afterwards, and afterwards is when an auditor asks.
    """

    is_trusted: bool = False
    press_observed: bool = False
    pointer_travel_px: int = 0

    def holds(self) -> bool:
        return self.is_trusted and self.press_observed


@dataclass
class AuthorizationRecord:
    authorization_type: str
    source_environment: str
    destination_environment: str
    source_branch: str
    destination_branch: str
    commit: str
    commit_subject: str
    commits_ahead: int
    operator: str
    decided_at: str
    verdict: str
    seconds_waited: int
    proof: dict = field(default_factory=dict)
    outcome: str = "pending"

    def to_document(self) -> dict:
        return asdict(self)


class PromoteAuthorizationWorker:
    """Ask the operator, record the answer, and only then let the ref move."""

    def __init__(self, facts: PromotionFacts, operator: str = "") -> None:
        self.facts = facts
        self.operator = operator or _ch_os.environ.get("USERNAME", "unknown")
        self._collection = None

    # -- the collection ---------------------------------------------------
    def collection(self):
        if self._collection is None:
            client = ChatHealthyMongoUtilities().getConnection(
                AUTHORIZATION_IDENTITY, AUTHORIZATION_CLUSTER)
            self._collection = (client[AUTHORIZATION_DATABASE]
                                [AUTHORIZATION_COLLECTION])
        return self._collection

    # -- the question -----------------------------------------------------
    def build_question(self) -> dict:
        f = self.facts
        return {
            "collection": f"{f.commit[:12]} — {f.commit_subject}",
            "source": {"cluster": f.source_environment,
                       "database": f.source_branch},
            "destination": {"cluster": f.destination_environment,
                            "database": f.destination_branch},
            "authorizer": self.operator,
        }

    # -- the gate ---------------------------------------------------------
    def authorize(self):
        f = self.facts
        if (f.source_environment, f.destination_environment) not in ADJACENT_PAIRS:
            raise ChatHealthyException(
                "promotion_not_adjacent",
                f"{f.source_environment} to {f.destination_environment} is not "
                f"an adjacent pair; promotion advances one step at a time",
                component="PromoteAuthorizationWorker")

        decision = request_authorization(
            "this promotion", f.subject(),
            timeout_seconds=TIMEOUT_SECONDS,
            palette="migration",
            banner=(f"Promote {f.source_environment} to "
                    f"{f.destination_environment}"),
            transfer=self.build_question())

        proof = HumanPresenceProof(
            is_trusted=bool(getattr(decision, "human_click", False)),
            press_observed=bool(getattr(decision, "human_click", False)),
            pointer_travel_px=int(getattr(decision, "pointer_travel_px", 0)))

        record = AuthorizationRecord(
            authorization_type=AUTHORIZATION_TYPE,
            source_environment=f.source_environment,
            destination_environment=f.destination_environment,
            source_branch=f.source_branch,
            destination_branch=f.destination_branch,
            commit=f.commit,
            commit_subject=f.commit_subject,
            commits_ahead=f.commits_ahead,
            operator=self.operator,
            decided_at=datetime.now(timezone.utc).isoformat(),
            verdict=decision.verdict,
            seconds_waited=int(getattr(decision, "seconds_waited", 0)),
            proof=asdict(proof))

        if not decision.approved:
            self._write(record, tolerate_failure=True)
            return None

        if not proof.holds():
            record.verdict = "reject"
            record.outcome = "refused_no_human_presence"
            self._write(record, tolerate_failure=True)
            return None

        # Written while the destination ref still points at the old commit.
        return self._write(record, tolerate_failure=False)

    # -- the record -------------------------------------------------------
    def _write(self, record: AuthorizationRecord, tolerate_failure: bool):
        try:
            result = self.collection().insert_one(record.to_document())
            return result.inserted_id
        except Exception as exc:  # noqa: BLE001 - converted at the boundary
            if tolerate_failure:
                return None
            raise ChatHealthyException(
                "authorization_unrecordable",
                f"the authorization could not be written to "
                f"{AUTHORIZATION_DATABASE}.{AUTHORIZATION_COLLECTION}: {exc}. "
                f"The promotion does not proceed; an authorization that left "
                f"no record did not occur.",
                component="PromoteAuthorizationWorker",
                exception=exc)

    def record_outcome(self, record_id, outcome: str) -> None:
        """Append what happened, rather than altering what was authorized.

        The record of an authorization is not edited once written - the same
        principle role_data_migrator is built on, where a role may add and may
        never alter. So the outcome is a second document naming the
        authorization it belongs to. Without it the record claims a promotion
        was authorized while the ref may never have moved.
        """
        if record_id is None:
            return
        f = self.facts
        document = {
            "authorization_type": "promotion_outcome",
            "authorization_id": record_id,
            "source_environment": f.source_environment,
            "destination_environment": f.destination_environment,
            "commit": f.commit,
            "outcome": outcome,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            self.collection().insert_one(document)
        except Exception as exc:  # noqa: BLE001
            _log.error(f"[promote] outcome '{outcome}' could not be recorded "
                       f"against {record_id}: {exc}")
