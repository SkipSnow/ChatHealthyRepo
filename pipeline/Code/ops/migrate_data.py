"""Ask the operator, then fire the migration runbook.

    python pipeline/Code/ops/migrate_data.py Provider_v_4

This file does not migrate anything. It opens a page in the operator's
browser naming the collection, blocks until they click APPROVE or REJECT,
records the answer, and — only on APPROVE — POSTs the webhook that starts
ChatHealthyJobManager's migration runbook.

The click is the sign-off EPIC-010-F-108-S-001-REQ-B-003 requires. It is
recorded here before the webhook is sent, so a migration that ran is always
preceded by the record of the human who released it, and the record names the
collection they were looking at when they clicked.

A rejection is recorded too. An operator who says no leaves the same trace as
one who says yes.
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
        sys.path.insert(0, str(_parent / "pipeline" / "Code"))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        break

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402
from chathealthy_lib.human_authorization import (  # noqa: E402
    APPROVE, request_authorization)
from chathealthy_lib.logging_service import (  # noqa: E402
    ChatHealthyLoggingService, set_mongo_log_identity)
from chathealthy_lib.mongo_utilities import ChatHealthyMongoUtilities  # noqa: E402

set_mongo_log_identity("pipelineEditor")

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


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize and fire one collection migration.")
    parser.add_argument("collection")
    parser.add_argument("--env", default="dev", choices=sorted(_VAULT_BY_ENV))
    args = parser.parse_args()

    vault = _VAULT_BY_ENV[args.env]
    subject = (f"{args.collection}\n"
               f"ChatHealthyDataPipelines.PipelinePublicHealthData"
               f"  →  ChatHealthyFrontEnd.PublicHealthData")

    _log.info("data_migration authorization requested collection=%s env=%s",
              args.collection, args.env)

    authorization = request_authorization(
        "this migration", subject,
        timeout_seconds=_AUTHORIZATION_TIMEOUT_SECONDS,
        palette="migration",
        banner=f"Data migration — {args.env}",
        detail=(
            f"APPROVE copies every document in <b>{args.collection}</b> onto "
            f"the cluster that serves users, under the same name, and builds "
            f"its indexes. It cannot overwrite or delete anything that is "
            f"already there, and nothing in the estate can remove what it "
            f"writes — so an approval here is not reversible by us. "
            f"REJECT stops now: nothing is copied and the refusal is recorded "
            f"against your name."))

    _log.info(
        "data_migration authorization %s collection=%s human_click=%s "
        "waited=%.1fs env=%s",
        authorization.verdict, args.collection, authorization.human_click,
        authorization.seconds_waited, args.env)

    # Every verdict is written to Mongo, not just the one that proceeds. A
    # refusal that leaves only a log line is a decision with no record: the
    # question of who was asked and what they said has to be answerable from
    # the database afterwards, and it has to be answerable the same way
    # whichever way they answered.
    released_at = datetime.datetime.now(datetime.timezone.utc)
    approval_id = f"migration-approval-{uuid.uuid4().hex}"
    approvals = ChatHealthyMongoUtilities().getConnection(
        "pipelineEditor", "ChatHealthyFrontEnd"
    )["pipelineAdmin"]["Authorizations"]
    approvals.insert_one({
        "_id": approval_id,
        "type": _AUTHORIZATION_TYPE,
        "collection": args.collection,
        "env": args.env,
        "verdict": authorization.verdict,
        "human_click": authorization.human_click,
        "subject": authorization.subject,
        "seconds_waited": authorization.seconds_waited,
        "released_at": released_at,
    })
    _log.info("data_migration decision recorded id=%s collection=%s verdict=%s",
              approval_id, args.collection, authorization.verdict)

    if not authorization.approved:
        _log.error("data_migration NOT authorized collection=%s verdict=%s "
                   "approval_id=%s; nothing was fired",
                   args.collection, authorization.verdict, approval_id)
        return 1

    # The payload carries only the record's id. It used to carry the verdict
    # itself, which meant the runbook believed whatever the caller asserted --
    # anyone who could reach the webhook could type human_click=true and
    # migrate without a person. Naming a record instead means forging one
    # requires pipelineEditor's certificate.

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


if __name__ == "__main__":
    raise SystemExit(main())
