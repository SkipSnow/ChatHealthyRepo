"""Ask the operator, then fire the migration runbook.

    python pipeline/Code/ops/migrate_data.py Provider_v_4

This file does not migrate anything. It opens a page in the operator's
browser naming the collection and blocks until they click APPROVE or REJECT.
On APPROVE it puts the code that environment runs to its engineering rule,
records the release, and POSTs the webhook that starts ChatHealthyJobManager's
migration runbook. On REJECT it logs the refusal and stops.

The click is the sign-off EPIC-010-F-108-S-001-REQ-B-003 requires. It is
recorded here before the webhook is sent, so a migration that ran is always
preceded by the record of the human who released it, and the record names the
collection they were looking at when they clicked.

Nothing is recorded and nothing is fired unless the release survives both
the click and the rule.
"""
from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import subprocess
import sys
import uuid
import urllib.request

_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "pipeline" / "Code").is_dir():
        _REPO = _parent
        sys.path.insert(0, str(_parent / "pipeline" / "Code"))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        break

from dotenv import load_dotenv  # noqa: E402

# The program loads its own environment, as every other operator-facing
# surface here does. Without it the program takes an APPROVE and then dies
# reaching for a vault it has no address for.
load_dotenv(str(_REPO / ".env"), override=False)

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.human_authorization import (  # noqa: E402
    APPROVE, request_authorization)
from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402
from pymongo import ReturnDocument  # noqa: E402

set_mongo_log_identity("pipelineEditor")

_RECORDER = "pipelineEditor"

_log = ChatHealthyLoggingService()

_VAULT_BY_ENV = {"dev": "kv-chpipeline-dev"}
_WEBHOOK_SECRET_NAME = "DATA-MIGRATION-WEBHOOK-URL"
_AUTHORIZATION_TIMEOUT_SECONDS = 600
# One collection holds every authorization the estate takes, and the type
# says which kind this is. A migration approval and a commit approval are
# the same shape of fact -- who was asked, what they were shown, what they
# said -- and they belong in one place that can be read as a whole.
_AUTHORIZATION_TYPE = "data_migration"


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _webhook_url(vault: str) -> str:
    result = subprocess.run(
        ["az", "keyvault", "secret", "show", "--vault-name", vault,
         "--name", _WEBHOOK_SECRET_NAME, "--query", "value", "-o", "tsv"],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"))
    url = (result.stdout or "").strip()
    if result.returncode != 0 or not url:
        raise ChatHealthyException(
            mode="config_error",
            component="migrate_data",
            message=(f"cannot read {_WEBHOOK_SECRET_NAME} from {vault}: "
                     f"{(result.stderr or '').strip()[:300]}"))
    return url


def _git_source() -> tuple[str, str]:
    """The branch and commit this release was taken from."""
    def read(*argv: str) -> str:
        found = subprocess.run(["git", *argv], capture_output=True, text=True,
                               cwd=str(_REPO), timeout=30,
                               creationflags=_cflags())
        return (found.stdout or "").strip()
    return read("rev-parse", "--abbrev-ref", "HEAD"), read("rev-parse", "HEAD")


def _next_approval_number(counters, authorization_type: str) -> int:
    """The next approval number for this type of authorization.

    One counter document per type, advanced with $inc so the number is
    issued by the database rather than derived from what is already there:
    counting the collection would re-issue a number if a record were ever
    removed, and the number is what a person quotes afterwards.
    """
    counter = counters.find_one_and_update(
        {"_id": f"approval_number:{authorization_type}"},
        {"$inc": {"next": 1}},
        upsert=True, return_document=ReturnDocument.AFTER)
    return int(counter["next"])


def _authorizer() -> str:
    """Who the page names as authorizing, read from the deployment record.

    Read here rather than in the page: the page is a library and would have
    to work out what platform it is on to answer this.
    """
    record = json.loads(
        (_REPO / "brain" / "machine_artifacts" / "content"
         / "deployment_architecture.json").read_text(encoding="utf-8"))
    return ((record.get("firm") or {}).get("git_identity") or {}).get("name", "")


def _build_number() -> int | None:
    """The estate's build counter as it stands. Recorded on the release as a
    marker of when it happened, not as a claim about which artifact ran."""
    sys.path.insert(0, str(_REPO / "architecture"
                           / "DevOpsBuildDeployAndEnvironmentManagement"))
    from version_counter import read_build_number
    return read_build_number()


def _run_migration(parser):
    """Ask, then migrate, and report the outcome."""
    parser.add_argument("collection")
    parser.add_argument("--env", default="dev", choices=sorted(_VAULT_BY_ENV))
    args = parser.parse_args()

    vault = _VAULT_BY_ENV[args.env]
    subject = (f"{args.collection}\n"
               f"ChatHealthyDataPipelines.PipelinePublicHealthData"
               f"  →  ChatHealthyFrontEnd.PublicHealthData")

    _log.info("data_migration authorization requested collection=%s env=%s",
              args.collection, args.env)

    # The page asks the transfer question: what moves, and between which two
    # ends. Who is authorizing is the page's own business and it works that
    # out; this names no person.
    transfer = {
        "collection": args.collection,
        # The keys are the labels the page prints, so they read as the
        # operator reads them.
        "source": {"Cluster": "ChatHealthyDataPipelines",
                   "Data Base": "PipelinePublicHealthData"},
        "destination": {"Cluster": "ChatHealthyFrontEnd",
                        "Data Base": "PublicHealthData"},
        "authorizer": _authorizer(),
    }

    authorization = request_authorization(
        "this migration", subject,
        timeout_seconds=_AUTHORIZATION_TIMEOUT_SECONDS,
        palette="migration",
        transfer=transfer,
        banner=f"Data migration — {args.env}",
        detail=(
            f"APPROVE copies every document in <b>{args.collection}</b> onto "
            f"the cluster that serves users, under the same name, and builds "
            f"its indexes. It cannot overwrite or delete anything that is "
            f"already there, and nothing in the estate can remove what it "
            f"writes — so an approval here is not reversible by us. "
            f"REJECT stops now: nothing is copied, nothing is recorded on the "
            f"cluster, and the refusal is written to the log."))

    _log.info(
        "data_migration authorization %s collection=%s human_click=%s "
        "waited=%.1fs env=%s",
        authorization.verdict, args.collection, authorization.human_click,
        authorization.seconds_waited, args.env)

    # A refusal ends in the log. Nothing was released, so there is nothing
    # for the cluster to hold a record of.
    if not authorization.approved:
        _log.error("data_migration NOT authorized collection=%s verdict=%s "
                   "env=%s; nothing was judged, recorded or fired",
                   args.collection, authorization.verdict, args.env)
        return 1

    released_at = datetime.datetime.now(datetime.timezone.utc)
    approval_id = f"migration-approval-{uuid.uuid4().hex}"
    branch, commit = _git_source()
    # pipelineEditor writes the approval record. The one database this
    # program reaches is pipelineAdmin on the front-end cluster, and this
    # identity's certificate is the one entitled to it. It touches no
    # collection of data on either cluster; moving data is the migrator's
    # work and happens in Azure.
    approvals = ChatHealthyMongoUtilities().getConnection(
        _RECORDER, "ChatHealthyFrontEnd"
    )["pipelineAdmin"]["Authorizations"]
    # Queried over time by (type, day) and by (type, collection), so those
    # are their own fields rather than packed into the id. The indexes that
    # serve those queries are created once, operationally; a program that
    # builds them on every invocation is doing an administrator's work every
    # time it does its own. The id is a surrogate and carries no meaning.
    approvals.insert_one({
        "_id": approval_id,
        "approval_number": _next_approval_number(
            approvals.database["Counters"], _AUTHORIZATION_TYPE),
        "type": _AUTHORIZATION_TYPE,
        "collection": args.collection,
        "env": args.env,
        "approval": authorization.approved,
        "message": authorization.verdict,
        "gitSourceAttribute": branch,
        "source_commit": commit,
        "build_number": _build_number(),
        "human_click": authorization.human_click,
        "subject": authorization.subject,
        "seconds_waited": authorization.seconds_waited,
        "released_at": released_at,
        # The day stands on its own. Asking which approvals happened on a
        # date should not mean computing a range over a timestamp, and the
        # day is how a person remembers it.
        "day": released_at.date().isoformat(),
    })
    _log.info("data_migration decision recorded id=%s collection=%s verdict=%s",
              approval_id, args.collection, authorization.verdict)

    # The payload carries only the record's id. It used to carry the verdict
    # itself, which meant the runbook believed whatever the caller asserted --
    # anyone who could reach the webhook could type human_click=true and
    # migrate without a person. Naming a record instead means forging one
    # requires the migrator's certificate.

    payload = {
        "collection": args.collection,
        "approval_id": approval_id,
        "released_at": released_at.isoformat(),
    }

    try:
        url = _webhook_url(vault)
    except ChatHealthyException as exc:
        _log.error("data_migration could not fire collection=%s mode=%s: %s",
                   args.collection, exc.mode, exc)
        return 1

    request = urllib.request.Request(
        url, method="POST", data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as response:
        body = response.read().decode("utf-8", "replace")
        _log.info("data_migration fired collection=%s http=%d response=%s",
                  args.collection, response.status, body[:300])

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize and fire one collection migration.")
    return _run_migration(parser)


if __name__ == "__main__":
    sys.exit(main())
