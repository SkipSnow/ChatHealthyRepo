"""ca_bootstrap_runbook.py - one-shot bootstrap of the ChatHealthy CA.

Runs in Azure Automation as a Python 3 runbook. Executed once, by the
operator, at CA stand-up time. Idempotent - re-running detects the
bootstrap marker in Key Vault and no-ops.

Steps (F-003 Design v1 sec 3):
  1. Read the bootstrap marker from KV. If present, exit 0 - already done.
  2. Generate the root CA keypair + self-signed root CA cert.
  3. Generate the intermediate CA keypair. Sign it with the root key.
  4. Write to Key Vault (kv-chpipeline-dev):
       ca-root-privatekey        - root private key PEM (will be taken
                                    cold by the operator after this run)
       ca-root-cert              - root cert PEM (public, chain-baked)
       ca-intermediate-privatekey - intermediate private key PEM (runtime
                                    signing key; only mi-runbook reads)
       ca-intermediate-cert      - intermediate cert PEM (public, chain-baked)
       ca-bootstrap-complete     - marker; presence -> already bootstrapped
  5. Exit 0.

Operator post-run duties (out of this script):
  - Export ca-root-privatekey to offline custody and delete or rotate
    the KV entry (F-003 Design v1 sec 3.1 - root goes cold).
  - Confirm KV RBAC scopes: only mi-runbook holds read on
    ca-intermediate-privatekey; the intermediate cert + root cert are
    publicly readable so images can bake them at build time.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import datetime
import json

import os
import socket
import sys
import traceback

# Ensure sibling ca_helpers.py resolves whether the runbook is run from
# the deploy dir or dropped into the Automation Account root.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ca_helpers as ch


# Hydrate deploy-pushed Automation Variables into os.environ. deploy_chain
# pushes each name in the secrets block on the DeploymentTargetRecord as
# an Automation Variable; without this hydration the runbook falls back
# to code defaults hardcoded for dev.
try:
    import automationassets as _aa
    for _k in ("KEY_VAULT_URI", "AUTOMATION_ENV_PREFIX"):
        try:
            os.environ[_k] = str(_aa.get_automation_variable(_k))
        except Exception:
            pass
except ImportError:
    pass


# Wire Mongo logging BEFORE ChatHealthyLoggingService() singleton is
# instantiated. KV Mongo secret fetch + SRV bypass so CHLS's Mongo
# handler wires to the front-end cluster (log to Log_{env}). Falls back
# to stderr-only on any failure so CA bootstrap never blocks on logging
# plumbing.
os.environ.setdefault("CH_SPACE_NAME", "ca-bootstrap")
os.environ.setdefault("CH_COMPONENT", "ca-bootstrap")
os.environ.setdefault("ENV_PREFIX",
                      os.environ.get("AUTOMATION_ENV_PREFIX", "dev"))
try:
    from chathealthy_lib.pipeline_boot import bootstrap_aa_mongo_logging as _boot
    _boot(
        component_name="ca-bootstrap",
        env_prefix=os.environ.get("AUTOMATION_ENV_PREFIX", "dev"),
    )
except Exception as _bootstrap_exc:  # noqa: BLE001
    sys.stderr.write(
        f"ca_bootstrap: bootstrap_aa_mongo_logging failed "
        f"({type(_bootstrap_exc).__name__}: {_bootstrap_exc}); "
        "continuing with stderr-only logging.\n"
    )
    os.environ["CH_LOG_DESTINATION"] = "stderr"


LOG = ChatHealthyLoggingService()


KEY_VAULT_URI = os.environ.get(
    "KEY_VAULT_URI", "https://kv-chpipeline-dev.vault.azure.net/"
)
_HOSTNAME = socket.gethostname()


def _log_event(event: str, **fields) -> None:
    now = datetime.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    record = {
        "ts": now,
        "runbook": "ca_bootstrap_runbook",
        "hostname": _HOSTNAME,
        "event": event,
    }
    record.update(fields)
    LOG.info(json.dumps(record, default=str))


def _already_bootstrapped(vault_uri: str) -> bool:
    marker = ch.kv_get(vault_uri, ch.KV_SECRET_BOOTSTRAP_MARKER)
    return marker is not None and marker.strip() != ""


def _bootstrap(vault_uri: str) -> None:
    _log_event("generate_root_ca_keypair")
    root_key = ch.generate_ca_keypair()
    root_cert = ch.build_root_ca_cert(root_key)
    _log_event("root_ca_created",
               subject=root_cert.subject.rfc4514_string(),
               not_after=ch.cert_not_after_iso(root_cert))

    _log_event("generate_intermediate_ca_keypair")
    inter_key = ch.generate_ca_keypair()
    inter_cert = ch.build_intermediate_ca_cert(inter_key, root_key, root_cert)
    _log_event("intermediate_ca_created",
               subject=inter_cert.subject.rfc4514_string(),
               not_after=ch.cert_not_after_iso(inter_cert))

    # Write four material secrets + marker.
    _log_event("kv_put_root_privatekey")
    ch.kv_put(vault_uri, ch.KV_SECRET_ROOT_CA_KEY,
              ch.key_to_pem(root_key).decode("ascii"),
              tags={"purpose": "root-ca-private-key",
                    "custody": "take-cold-after-bootstrap"})
    _log_event("kv_put_root_cert")
    ch.kv_put(vault_uri, ch.KV_SECRET_ROOT_CA_CERT,
              ch.cert_to_pem(root_cert).decode("ascii"),
              tags={"purpose": "root-ca-cert", "visibility": "public"})
    _log_event("kv_put_intermediate_privatekey")
    ch.kv_put(vault_uri, ch.KV_SECRET_INTERMEDIATE_KEY,
              ch.key_to_pem(inter_key).decode("ascii"),
              tags={"purpose": "intermediate-ca-private-key",
                    "readable-by": "mi-runbook"})
    _log_event("kv_put_intermediate_cert")
    ch.kv_put(vault_uri, ch.KV_SECRET_INTERMEDIATE_CERT,
              ch.cert_to_pem(inter_cert).decode("ascii"),
              tags={"purpose": "intermediate-ca-cert", "visibility": "public"})

    marker = json.dumps({
        "bootstrapped_at": datetime.datetime.utcnow().isoformat() + "Z",
        "hostname": _HOSTNAME,
    })
    ch.kv_put(vault_uri, ch.KV_SECRET_BOOTSTRAP_MARKER, marker,
              tags={"purpose": "ca-bootstrap-marker"})
    _log_event("kv_put_bootstrap_marker")


def main() -> int:
    vault_uri = KEY_VAULT_URI
    _log_event("runbook_start", vault_uri=vault_uri)
    try:
        if _already_bootstrapped(vault_uri):
            _log_event("already_bootstrapped_noop")
            return 0
        _bootstrap(vault_uri)
        _log_event("bootstrap_complete")
        return 0
    except Exception as exc:
        _log_event("bootstrap_failed",
                   error_type=type(exc).__name__,
                   error_msg=str(exc),
                   traceback=traceback.format_exc()[-2000:])
        return 1


if __name__ == "__main__":
    sys.exit(main())
