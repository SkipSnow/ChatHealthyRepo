"""EPIC-008-F-012-S-004-REQ-B-006 - a deployment is approved by a human.

Every deployment is approved before anything is deployed, the approval is
recorded, and the outcome is recorded against it. A refusal is not a failure
of the deployment; it is a rejected deployment, and it is recorded as one.

Nothing about a deploy was gated before this. Promote asked and recorded;
deploy asked nothing, so the environments that serve users changed on no
authority anyone could later point at.

One deployment is one authorization, whatever it touches. A deploy names
several targets and they do not share a fate -- one can fail while another
succeeds and a third is skipped to protect the contract -- so the outcome
record names what happened to each, rather than pretending the invocation
had a single result.
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

import os as _ch_os  # noqa: E402

_ch_os.environ.setdefault("CH_LOG_DESTINATION", "stderr")

if str(_pl.Path(__file__).resolve().parent) not in _sys.path:
    _sys.path.insert(0, str(_pl.Path(__file__).resolve().parent))

from dataclasses import dataclass, asdict, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from chathealthy_lib.human_authorization import request_authorization  # noqa: E402
from chathealthy_lib.logging_service import ChatHealthyLoggingService  # noqa: E402

import authorization_record  # noqa: E402

_log = ChatHealthyLoggingService()

AUTHORIZATION_TYPE = "deployment"
OUTCOME_TYPE = "deployment_outcome"
TIMEOUT_SECONDS = 600


@dataclass(frozen=True)
class DeploymentFacts:
    """The identity of one deployment.

    These reach the operator on the page and the record in the same shape, so
    the thing approved and the thing recorded cannot differ.
    """

    environment: str
    targets: list[str]
    packages: dict[str, list[str]]
    build_number: int
    commit: str

    def subject(self) -> str:
        return (f"{self.environment}: build {self.build_number} "
                f"({self.commit[:12]}) to {len(self.targets)} target(s)")


@dataclass
class HumanPresenceProof:
    """Evidence that a person, not a program, answered."""

    is_trusted: bool = False
    press_observed: bool = False

    def holds(self) -> bool:
        return self.is_trusted and self.press_observed


@dataclass
class DeploymentAuthorization:
    authorization_type: str
    environment: str
    targets: list[str]
    packages: dict[str, list[str]]
    build_number: int
    commit: str
    operator: str
    decided_at: str
    verdict: str
    seconds_waited: int
    proof: dict = field(default_factory=dict)

    def to_document(self) -> dict:
        return asdict(self)


class DeployAuthorizationWorker:
    """Ask the operator, record the answer, and only then let a deploy run."""

    def __init__(self, facts: DeploymentFacts, operator: str = "") -> None:
        self.facts = facts
        self.operator = operator or _ch_os.environ.get("USERNAME", "unknown")
        self.last_verdict = "not asked"

    def build_question(self) -> dict:
        """What the operator is being asked, in a deployment's own words."""
        f = self.facts
        listed = ", ".join(f.targets) if len(f.targets) <= 3 else (
            f"{len(f.targets)} targets")
        return {
            "collection": listed,
            "chip_label": "These targets will be changed",
            "source": {"Deploying": f"build {f.build_number}",
                       "From commit": f.commit[:12] or "(unknown)"},
            "destination": {"Environment": f.environment,
                            "Targets": str(len(f.targets)),
                            "Packages": str(sum(len(v) for v in f.packages.values()))},
            "authorizer": self.operator,
        }

    def _detail(self) -> str:
        f = self.facts
        rows = "; ".join(f"{t}: {', '.join(f.packages.get(t, []))}"
                         for t in f.targets)
        return (f"<b>APPROVE</b> installs build {f.build_number} into "
                f"<b>{f.environment}</b>. {rows}. <b>REJECT</b> deploys "
                f"nothing and is recorded as a rejected deployment.")

    def authorize(self):
        """Return the record id, or None when the deployment is not approved."""
        f = self.facts
        decision = request_authorization(
            "this deployment", f.subject(),
            timeout_seconds=TIMEOUT_SECONDS,
            palette="entitlement",
            banner=f"Deploy to {f.environment}",
            detail=self._detail(),
            transfer=self.build_question())

        self.last_verdict = decision.verdict
        proof = HumanPresenceProof(
            is_trusted=bool(getattr(decision, "human_click", False)),
            press_observed=bool(getattr(decision, "human_click", False)))

        record = DeploymentAuthorization(
            authorization_type=AUTHORIZATION_TYPE,
            environment=f.environment,
            targets=list(f.targets),
            packages=dict(f.packages),
            build_number=f.build_number,
            commit=f.commit,
            operator=self.operator,
            decided_at=datetime.now(timezone.utc).isoformat(),
            verdict=decision.verdict,
            seconds_waited=int(getattr(decision, "seconds_waited", 0)),
            proof=asdict(proof))

        # A refusal is an answer the operator gave, and it is recorded with
        # the same intolerance as an approval: a rejected deployment nobody
        # can find later is indistinguishable from one never asked about.
        if not decision.approved or not proof.holds():
            document = record.to_document()
            document["outcome"] = ("rejected" if decision.verdict == "reject"
                                   else f"rejected_{decision.verdict}")
            authorization_record.append(document, tolerate_failure=False)
            return None

        # Written before anything is deployed.
        return authorization_record.append(record.to_document(),
                                           tolerate_failure=False)

    def record_outcome(self, record_id, outcome: str,
                       per_target: dict | None = None) -> None:
        """Append what happened, rather than altering what was authorized.

        The outcome is a second document naming the authorization it belongs
        to. Without it the record claims a deployment was authorized and says
        nothing about whether it happened.
        """
        document = {
            "authorization_type": OUTCOME_TYPE,
            "authorization_id": record_id,
            "environment": self.facts.environment,
            "build_number": self.facts.build_number,
            "commit": self.facts.commit,
            "outcome": outcome,
            "per_target": per_target or {},
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            authorization_record.append(document, tolerate_failure=True)
        except Exception as exc:  # noqa: BLE001 - reported, never raised
            _log.error(f"[deploy] outcome '{outcome}' could not be recorded "
                       f"against {record_id}: {exc}")
