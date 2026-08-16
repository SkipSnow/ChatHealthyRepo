"""Container bootstrap for the pipeline tier.

Single boot path for all identities (long-lived and workers), dispatched on
the node's canonical identity (env var CHATHEALTHY_NODE_IDENTITY):

All pipeline identities (pipeline-runbook, pipeline-control,
pipeline-worker-*, and any future pipeline service):
  - Attached managed identity is the seed credential.
  - Seed-read the leaf cert + private key from Key Vault at the consolidated
    path certs/pipelineEditor using the MI (F-003 §5.1, §6.1). All pipeline
    identities share this single pre-provisioned cert. Materialize as PEM
    files with 0600 perms.
  - Load every other secret this node needs from KV via the same MI.
  - Load the CA public cert + intermediate chain from KV so downstream
    Mongo X.509 clients can verify server (F-003 §7.1).

Single path:
  - Set env vars for downstream code:
      CHATHEALTHY_NODE_IDENTITY   (already present; unchanged)
      CHATHEALTHY_CERT_PATH       (path to leaf cert PEM)
      CHATHEALTHY_KEY_PATH        (path to private key PEM)
      CHATHEALTHY_CA_CHAIN_PATH   (path to concatenated CA chain PEM)
  - Register cleanup so materialized cert/key/chain files are deleted on
    process exit.
  - exec argv[1] with argv[2:] as forwarded args.
"""
from __future__ import annotations
from chathealthy_lib.logging_service import (
    ChatHealthyLoggingService,
    set_mongo_log_identity,
)

# Whose certificate the log handler connects with. Every other pipeline module
# picks this up by importing pipeline_db; bootstrap does not import it, so
# without this line the first ChatHealthyLoggingService call raises
# mongo_log_identity_not_set -- which is how the container died even after it
# could reach the vault.
set_mongo_log_identity("pipelineEditor")

import atexit
import os
import socket
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple

from azure.identity import ClientSecretCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient

import blob_logger
import chathealthy_ca
from PipelineServices.observability_gate import ObservabilityGate
from chathealthy_lib.exceptions import ChatHealthyException


# Environment inputs the deploy step provides on every container.
ENV_NODE_IDENTITY = "CHATHEALTHY_NODE_IDENTITY"
ENV_KEY_VAULT_URI = "KEY_VAULT_URI"

# Environment outputs bootstrap sets for downstream code (pymongo, etc.).
ENV_CERT_PATH = "CHATHEALTHY_CERT_PATH"
ENV_KEY_PATH = "CHATHEALTHY_KEY_PATH"
ENV_CA_CHAIN_PATH = "CHATHEALTHY_CA_CHAIN_PATH"

# KV path convention. Identity certificates are pre-provisioned under the
# canonical per-identity names the library uses (chathealthy_lib.mongo_utilities
# _VAULT_CERT_KEY / _VAULT_PRIVATE_KEY), so the runtime and the library read the
# same material. Nothing here mints: certificate issuance belongs to
# claudeCodeAgent, and a node that cannot find its cert fails rather than
# creating one.
KV_CERT_PREFIX = "cert-"            # cert-pipelineEditor
KV_KEY_PREFIX = "key-"              # key-pipelineEditor
KV_CA_PREFIX = "ca-"                # every CA secret, public and private
KV_LEGACY_CERT_PREFIX = "certs-"    # superseded certs-pipeline-* secrets
KV_CA_PUBLIC = "ca-root-cert"
KV_CA_INTERMEDIATE_CHAIN = "ca-intermediate-cert"


PIPELINE_IDENTITY = "pipelineEditor"


def _pipeline_credential():
    """The credential this node opens the vault with.

    The mechanism follows what the identity IS. pipelineEditor is an
    application registration and proves itself with its client secret; a
    managed identity is proven by the machine it is attached to.

    This asked the instance metadata endpoint unconditionally. The run host
    stopped carrying a managed identity when pipelineEditor became the runtime
    identity, so every container died on its first vault call with
    "ManagedIdentityCredential authentication unavailable, no response from
    the IMDS endpoint" -- before it could log, before it could write a
    heartbeat, before it could say anything at all. Its own credentials were
    in the environment, unread.
    """
    tenant = os.environ.get(f"{PIPELINE_IDENTITY.upper()}_AZURE_TENANT_ID", "").strip()
    client_id = os.environ.get(f"{PIPELINE_IDENTITY.upper()}_AZURE_CLIENT_ID", "").strip()
    secret = os.environ.get(f"{PIPELINE_IDENTITY.upper()}_AZURE_CLIENT_SECRET", "").strip()
    if secret:
        if not (tenant and client_id):
            _emit(
                f"FATAL: {PIPELINE_IDENTITY} has a client secret but is missing "
                f"tenant or client id; cannot open the vault as anyone."
            )
            raise ChatHealthyException(
                mode="identity_credential_incomplete",
                component="PipelineBootstrap",
                message=(f"{PIPELINE_IDENTITY}: client secret present, "
                         f"tenant={bool(tenant)} client_id={bool(client_id)}"))
        _emit(f"vault credential: {PIPELINE_IDENTITY} client secret")
        return ClientSecretCredential(
            tenant_id=tenant, client_id=client_id, client_secret=secret)
    mi_client_id = os.environ.get("AZURE_CLIENT_ID", "").strip()
    _emit(f"vault credential: managed identity"
          f"{' (' + mi_client_id + ')' if mi_client_id else ''}")
    return (ManagedIdentityCredential(client_id=mi_client_id) if mi_client_id
            else ManagedIdentityCredential())


def _kv_name_to_env_key(kv_name: str) -> str:
    return kv_name.replace("-", "_")


def _emit(msg: str) -> None:
    """Bootstrap-only stdout channel. Rule-005 exempts the bootstrap
    entry-point from the ChatHealthy logging service because it runs
    before the logging service can be initialized (KV creds still being
    fetched)."""
    sys.stdout.write(f"[bootstrap] {msg}\n")
    sys.stdout.flush()


def _write_secure_file(data: bytes, suffix: str) -> Path:
    """Write bytes to a 0600 temp file. Returns the path."""
    fd, path_str = tempfile.mkstemp(suffix=suffix, prefix="chathealthy_")
    path = Path(path_str)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        # Windows local dev has looser semantics; deploy target is Linux
        # where the chmod is meaningful.
        pass
    return path


_materialized_paths: list[Path] = []

# Set by whatever spawns detached children that keep using this node's
# materialised credential. Workers are double-forked and reparented to init,
# so they outlive the Controller; deleting the certificate on the Controller's
# way out takes their credential with it, and they fail one by one with no
# way to say why. Nothing is deleted while that flag stands.
ENV_DETACHED_CHILDREN = "CHATHEALTHY_DETACHED_CHILDREN"


def _cleanup_materialized() -> None:
    if os.environ.get(ENV_DETACHED_CHILDREN, "").strip():
        _emit(
            f"materialised credentials left in place: "
            f"{os.environ.get(ENV_DETACHED_CHILDREN)} detached child process(es) "
            f"still hold them. The host is torn down at run end, which is what "
            f"removes them."
        )
        return
    for p in _materialized_paths:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


def _track(path: Path) -> Path:
    _materialized_paths.append(path)
    return path


def _resolve_node_identity() -> str:
    ident = os.environ.get(ENV_NODE_IDENTITY, "").strip()
    if not ident:
        _emit(
            f"FATAL: {ENV_NODE_IDENTITY} not set; deploy MUST inject "
            f"this env var on every pipeline container."
        )
        sys.exit(2)
    return ident


def _resolve_vault_uri() -> str:
    vault_uri = os.environ.get(ENV_KEY_VAULT_URI, "").strip()
    if not vault_uri:
        _emit(f"FATAL: {ENV_KEY_VAULT_URI} env var is required.")
        sys.exit(2)
    return vault_uri


ENV_SECRET_NAMES = "PIPELINE_SECRET_NAMES"


def _load_all_secrets_into_env(client: SecretClient) -> int:
    """Fetch the secrets this node is declared to need, and only those.

    The names arrive in PIPELINE_SECRET_NAMES, comma-separated, placed there
    by the deploy from what the manifest declares for this package. Names are
    not secrets, so they travel freely.

    This node used to enumerate the vault and take everything it found, which
    required read over the whole vault -- including certificate-authority
    private keys it has no business seeing. Fetching a declared list means the
    node can be granted exactly those secrets and nothing else, so a
    compromised run host yields only what that run legitimately used.

    A node that cannot read a secret it was declared to need fails here rather
    than starting without it and failing somewhere less obvious.
    """
    declared = [n.strip() for n in
                os.environ.get(ENV_SECRET_NAMES, "").split(",") if n.strip()]
    if not declared:
        _emit(f"FATAL: {ENV_SECRET_NAMES} is empty; the deploy MUST declare "
              f"which secrets this node reads.")
        raise ChatHealthyException(
            mode="pipeline_secret_names_absent",
            component="PipelineBootstrap",
            message=f"{ENV_SECRET_NAMES} absent or empty",
            context={"node": os.environ.get(ENV_NODE_IDENTITY, "")})

    # Key material never lands in os.environ: identity certificates and their
    # keys are materialised as files, and certificate-authority material is
    # never read by this node at all.
    skip_prefixes = (
        KV_CERT_PREFIX, KV_KEY_PREFIX, KV_LEGACY_CERT_PREFIX, KV_CA_PREFIX,
    )
    n = 0
    for name in declared:
        if any(name.startswith(pre) for pre in skip_prefixes):
            continue
        s = client.get_secret(name)
        tag = (s.properties.tags or {}).get("env_key")
        os.environ[tag if tag else _kv_name_to_env_key(name)] = s.value or ""
        n += 1
    return n


def _materialize_ca_chain(
    client: Optional[SecretClient],
) -> Path:
    """Return a path to the concatenated CA chain PEM.

    Prefer the image-baked chain at /etc/chathealthy/ca/ (deploy places
    it via ADD in the Dockerfile). Fall back to KV read via MI when the
    baked chain is absent (local dev / not-yet-baked images)."""
    intermediate_path = Path(chathealthy_ca.CA_INTERMEDIATE_CERT_PATH)
    root_path = Path(chathealthy_ca.CA_ROOT_CERT_PATH)
    if intermediate_path.is_file() and root_path.is_file():
        # Concatenate on disk into one PEM that pymongo's tlsCAFile
        # option accepts. Preserve trailing newline.
        chain_pem = intermediate_path.read_bytes()
        if not chain_pem.endswith(b"\n"):
            chain_pem += b"\n"
        chain_pem += root_path.read_bytes()
        out = _track(_write_secure_file(chain_pem, suffix="_ca_chain.pem"))
        _emit(f"CA chain materialized from image bake -> {out}")
        return out

    if client is None:
        _emit(
            "FATAL: image-baked CA chain not present and no KV client "
            "available to fall back to."
        )
        sys.exit(2)

    _emit("CA chain not baked into image; falling back to KV read")
    intermediate = client.get_secret(KV_CA_INTERMEDIATE_CHAIN).value or ""
    root = client.get_secret(KV_CA_PUBLIC).value or ""
    combined = intermediate.encode("utf-8")
    if not combined.endswith(b"\n"):
        combined += b"\n"
    combined += root.encode("utf-8")
    out = _track(_write_secure_file(combined, suffix="_ca_chain.pem"))
    _emit(f"CA chain materialized from KV -> {out}")
    return out


def _seed_read_long_lived_cert(
    client: SecretClient,
    node_identity: str,
) -> Tuple[Path, Path]:
    """F-003 §5.1 step 6: at container boot the long-lived target reads
    its cert + key from KV under the consolidated path via its own MI.
    All pipeline identities (pipeline-runbook, pipeline-control) read from
    the same cert secret. Returns (cert_path, key_path)."""
    # All pipeline identities run as pipelineEditor and read its cert.
    cert_secret = f"{KV_CERT_PREFIX}pipelineEditor"
    key_secret = f"{KV_KEY_PREFIX}pipelineEditor"
    _emit(f"seed-reading long-lived cert from KV: {cert_secret}")
    cert_pem = client.get_secret(cert_secret).value or ""
    key_pem = client.get_secret(key_secret).value or ""
    if not cert_pem or not key_pem:
        _emit(
            f"FATAL: KV secret {cert_secret!r} or {key_secret!r} is "
            f"empty; deploy MUST place both before this container "
            f"starts."
        )
        sys.exit(2)
    cert_path = _track(_write_secure_file(cert_pem.encode("utf-8"), "_cert.pem"))
    key_path = _track(_write_secure_file(key_pem.encode("utf-8"), "_key.pem"))
    return cert_path, key_path




def _announce_alive(node_identity: str) -> None:
    """Say, durably, that this container reached the point of speech.

    The credential that authenticates to Mongo is itself a vault secret, so
    this is the earliest instant the process can speak at all. Nothing before
    it can be recorded anywhere that outlives the container.
    """
    detail = (
        f"run_id={os.environ.get('RUN_ID', '<unset>')} "
        f"node={node_identity} "
        f"env={os.environ.get('ENV_PREFIX', '<unset>')} "
        f"data_version={os.environ.get('DATA_VERSION', '<unset>')} "
        f"load_mode={os.environ.get('LOAD_MODE', '<unset>')} "
        f"state_scope={os.environ.get('STATE_SCOPE', '<unset>')} "
        f"host={socket.gethostname()}"
    )
    try:
        from chathealthy_lib.logging_service import (  # noqa: PLC0415
            ChatHealthyLoggingService, set_mongo_log_identity)
        set_mongo_log_identity(PIPELINE_IDENTITY)
        os.environ["CH_LOG_DESTINATION"] = "stderr,mongo"
        ChatHealthyLoggingService().info(
            "bootstrap: container alive and able to log | %s", detail)
        _emit("bootstrap: announced to mongo")
        return
    except Exception as exc:  # noqa: BLE001
        _emit(f"bootstrap: CANNOT LOG TO MONGO ({type(exc).__name__}: {exc})")
        _mail_unreachable_mongo(detail, exc)
        _raise_cannot_log(detail, exc)


def _mail_unreachable_mongo(detail: str, exc: Exception) -> None:
    """The only channel left on a host that is about to be deleted."""
    try:
        from chathealthy_lib.notification_client import (  # noqa: PLC0415
            NotificationClient)
        to = os.environ.get("NOTIFICATION_TO_EMAIL", "").strip()
        if not to:
            _emit("bootstrap: no NOTIFICATION_TO_EMAIL; the alarm cannot be raised")
            return
        lines = [
            "A pipeline controller started and could not reach Mongo.",
            "",
            detail,
            "",
            f"{type(exc).__name__}: {exc}",
            "",
            "The run is abandoned. The container runs under docker --rm on a "
            "host deleted at the end of the run, so this mail is the only "
            "record that survives.",
        ]
        NotificationClient().send_email(
            to=to,
            subject="ChatHealthy pipeline FATAL: controller cannot log to Mongo",
            body=chr(10).join(lines),
        )
        _emit("bootstrap: alarm mailed")
    except Exception as mail_exc:  # noqa: BLE001
        _emit(f"bootstrap: mail also failed "
              f"({type(mail_exc).__name__}: {mail_exc})")


def _raise_cannot_log(detail: str, exc: Exception) -> None:
    """Raise-only helper: the catcher logs, not the thrower."""
    raise ChatHealthyException(
        mode="observability_unavailable",
        component="bootstrap",
        message=(f"controller cannot log to Mongo, so nothing it does would be "
                 f"recorded: {type(exc).__name__}: {exc} | {detail}"))


def main() -> int:
    pipeline_name = os.environ.get("PIPELINE_NAME", "provider")
    blob_logger.install(pipeline_name)

    # Observability gate call is DEFERRED to after KV secret load below
    # (Control path only). CH_SPACE_NAME + ENV_PREFIX are only in
    # os.environ AFTER _load_all_secrets_into_env populates them. Running
    # the gate here would abend on mode='mongo_env_unset' with no useful
    # signal about actual Mongo reachability.
    import socket  # noqa: PLC0415
    import traceback  # noqa: PLC0415

    def _dump_obs_abend(_obs_exc: ChatHealthyException) -> None:
        chls = ChatHealthyLoggingService()
        chls.error("=" * 78)
        chls.error("bootstrap: pipeline observability gate FAILED -- abending")
        chls.error("  pipeline_name (component): %r", pipeline_name)
        chls.error("  execution/server:          %r",
                   os.environ.get('CONTAINER_APP_JOB_EXECUTION_NAME',
                                  socket.gethostname()))
        chls.error("  env ENV_PREFIX:            %r",
                   os.environ.get('ENV_PREFIX', '<unset>'))
        chls.error("  env CH_SPACE_NAME:         %r",
                   os.environ.get('CH_SPACE_NAME', '<unset>'))
        chls.error("  mode:      %r", _obs_exc.mode)
        chls.error("  message:   %s", _obs_exc.message)
        chls.error("  server:    %r", _obs_exc.server)
        chls.error("  component: %r", _obs_exc.component)
        for _k, _v in (_obs_exc.context or {}).items():
            chls.error("  ctx.%s: %r", _k, _v)
        if _obs_exc.exception is not None:
            _orig = _obs_exc.exception
            chls.error("  original (chained) exception:")
            chls.error("    type: %s", type(_orig).__name__)
            chls.error("    args: %r", _orig.args)
            chls.error("    repr: %r", _orig)
            if _orig.__traceback__ is not None:
                chls.error("    original traceback:\n%s",
                           "".join(traceback.format_tb(_orig.__traceback__)))
        chls.error("  construction_stack (ChatHealthyException):")
        chls.error("%s", _obs_exc.construction_stack)
        chls.error("  live traceback:\n%s", traceback.format_exc())
        chls.error("=" * 78)

    if len(sys.argv) < 2:
        _emit("usage: bootstrap.py <entry_point.py> [args...]")
        return 2

    atexit.register(_cleanup_materialized)

    # What this container was handed, before anything can fail on it. Names
    # and presence only -- never a value. Every container that died today
    # died before it could say which of these was missing, and each cost a
    # deploy cycle to guess at.
    _emit("bootstrap: begin")
    _emit(
        "bootstrap: environment | "
        f"node={os.environ.get(ENV_NODE_IDENTITY, '<unset>')!r} "
        f"env_prefix={os.environ.get('ENV_PREFIX', '<unset>')!r} "
        f"run_id={os.environ.get('RUN_ID', '<unset>')!r} "
        f"data_version={os.environ.get('DATA_VERSION', '<unset>')!r} "
        f"load_mode={os.environ.get('LOAD_MODE', '<unset>')!r} "
        f"state_scope={os.environ.get('STATE_SCOPE', '<unset>')!r}"
    )
    _emit(
        "bootstrap: credentials | "
        f"vault_uri_set={bool(os.environ.get(ENV_KEY_VAULT_URI, '').strip())} "
        f"pipelineEditor_tenant={bool(os.environ.get('PIPELINEEDITOR_AZURE_TENANT_ID', '').strip())} "
        f"pipelineEditor_client_id={bool(os.environ.get('PIPELINEEDITOR_AZURE_CLIENT_ID', '').strip())} "
        f"pipelineEditor_secret={bool(os.environ.get('PIPELINEEDITOR_AZURE_CLIENT_SECRET', '').strip())} "
        f"declared_secret_count={len([n for n in os.environ.get(ENV_SECRET_NAMES, '').split(',') if n.strip()])} "
        f"ch_log_db={os.environ.get('CH_LOG_DB', '<unset>')!r}"
    )

    node_identity = _resolve_node_identity()
    _emit(f"node identity: {node_identity}")

    # The vault is opened as pipelineEditor, which proves itself with its
    # client secret. The certificate fetched here is what authenticates to
    # Mongo; the secret opens the vault and nothing else.
    vault_uri = _resolve_vault_uri()
    cred = _pipeline_credential()
    client = SecretClient(vault_url=vault_uri, credential=cred)
    _emit(f"KV client bound to {vault_uri}")
    cert_path, key_path = _seed_read_long_lived_cert(client, node_identity)
    _emit(f"identity certificate materialised for {node_identity}")
    n = _load_all_secrets_into_env(client)
    _emit(f"loaded {n} non-cert secrets into env")

    # Earliest possible moment: the Mongo credential is itself a vault
    # secret. Being unable to log is fatal -- a run nobody can explain is
    # worse than a run that did not start.
    _announce_alive(node_identity)
    ca_path = _materialize_ca_chain(client=client)
    _emit("CA chain ready; handing off to the entry point")

    os.environ[ENV_CERT_PATH] = str(cert_path)
    os.environ[ENV_KEY_PATH] = str(key_path)
    os.environ[ENV_CA_CHAIN_PATH] = str(ca_path)
    _emit(
        f"materialized: cert={cert_path.name} key={key_path.name} "
        f"ca_chain={ca_path.name}"
    )

    # Observability gate NOW: KV secrets have populated os.environ so
    # CH_SPACE_NAME (if declared) is present and the gate can prove
    # Mongo + Storage connectivity.
    # Any failure -> dump detail to stderr and abend with exit 1 so
    # AA/ACA marks the container Failed.
    try:
        ObservabilityGate(
            component=pipeline_name,
            server=os.environ.get(
                "CONTAINER_APP_JOB_EXECUTION_NAME", socket.gethostname()
            ),
        ).check()
    except ChatHealthyException as _obs_exc:
        _dump_obs_abend(_obs_exc)
        return 1

    entry_point = sys.argv[1]
    forward_args = sys.argv[2:]
    _emit(f"exec python {entry_point} {' '.join(forward_args)}")
    # execvp replaces the current process; atexit cleanup MUST run
    # before exec fires. Materialize a shim: call cleanup here for the
    # long-lived path? No — cert/key files MUST persist across the
    # exec because the entry point (control_runner, worker_runner)
    # reads them. Cleanup fires only when the whole container process
    # tree ends.
    os.execvp("python", ["python", entry_point, *forward_args])
    return 0  # unreachable


if __name__ == "__main__":
    raise SystemExit(main())
