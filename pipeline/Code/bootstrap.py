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
from chathealthy_lib.logging_service import ChatHealthyLoggingService

import atexit
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple

from azure.identity import ClientSecretCredential
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


def _pipeline_editor_credential():
    """The credential the pipeline authenticates with, everywhere.

    The three values are delivered to the job the same way every other secret
    is. A node that cannot present pipelineEditor does not start: there is no
    second identity to fall back to, because falling back to another identity
    is precisely what this removes.
    """
    keys = (f"{PIPELINE_IDENTITY.upper()}_AZURE_TENANT_ID",
            f"{PIPELINE_IDENTITY.upper()}_AZURE_CLIENT_ID",
            f"{PIPELINE_IDENTITY.upper()}_AZURE_CLIENT_SECRET")
    missing = [k for k in keys if not os.environ.get(k)]
    if missing:
        _emit(f"FATAL: cannot authenticate as {PIPELINE_IDENTITY}; "
              f"missing {', '.join(missing)}")
        raise ChatHealthyException(
            mode="azure_credential_missing",
            component="PipelineBootstrap",
            message=f"cannot authenticate as {PIPELINE_IDENTITY}: "
                    f"{', '.join(missing)} absent from the environment",
            context={"identity": PIPELINE_IDENTITY, "missing": missing})
    return ClientSecretCredential(
        tenant_id=os.environ[keys[0]],
        client_id=os.environ[keys[1]],
        client_secret=os.environ[keys[2]])


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


def _cleanup_materialized() -> None:
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


def _load_all_secrets_into_env(client: SecretClient) -> int:
    """Enumerate every secret in the vault and hydrate as env vars using
    the same dash->underscore mapping the existing pipeline code
    expects. Cert / CA / key secrets are handled separately and excluded
    here so their raw PEM never lands in os.environ."""
    n = 0
    # Key material never lands in os.environ: identity certs and keys, the
    # superseded certs-* secrets, and every ca-* secret including the private
    # keys this node has no grant to read.
    exclude_prefixes = (
        KV_CERT_PREFIX, KV_KEY_PREFIX, KV_LEGACY_CERT_PREFIX, KV_CA_PREFIX,
    )
    for props in client.list_properties_of_secrets():
        name = props.name
        if any(name.startswith(p) for p in exclude_prefixes):
            continue
        s = client.get_secret(name)
        tag = (s.properties.tags or {}).get("env_key")
        env_key = tag if tag else _kv_name_to_env_key(name)
        os.environ[env_key] = s.value or ""
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




def main() -> int:
    pipeline_name = os.environ.get("PIPELINE_NAME", "provider")
    blob_logger.install(pipeline_name)

    # Observability gate call is DEFERRED to after KV secret load below
    # (Control path only). MONGO_FRONTEND_connectionString + CH_SPACE_NAME
    # + ENV_PREFIX are only in os.environ AFTER _load_all_secrets_into_env
    # populates them. Running the gate here would abend on
    # mode='mongo_env_unset' with no useful signal about actual Mongo
    # reachability.
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
        chls.error("  env MONGO_FRONTEND_conn:   %s",
                   'set' if os.environ.get('MONGO_FRONTEND_connectionString')
                   else '<UNSET>')
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

    node_identity = _resolve_node_identity()
    _emit(f"node identity: {node_identity}")

    # The pipeline runs as pipelineEditor, and opens the vault as itself. Its
    # certificate is what authenticates to Mongo, so the identity that fetches
    # the certificate and the identity that uses it are one and the same.
    vault_uri = _resolve_vault_uri()
    cred = _pipeline_editor_credential()
    client = SecretClient(vault_url=vault_uri, credential=cred)
    _emit(f"KV client bound to {vault_uri}")
    cert_path, key_path = _seed_read_long_lived_cert(client, node_identity)
    n = _load_all_secrets_into_env(client)
    _emit(f"loaded {n} non-cert secrets into env")
    ca_path = _materialize_ca_chain(client=client)

    os.environ[ENV_CERT_PATH] = str(cert_path)
    os.environ[ENV_KEY_PATH] = str(key_path)
    os.environ[ENV_CA_CHAIN_PATH] = str(ca_path)
    _emit(
        f"materialized: cert={cert_path.name} key={key_path.name} "
        f"ca_chain={ca_path.name}"
    )

    # Observability gate NOW: KV secrets have populated os.environ so
    # MONGO_FRONTEND_connectionString (and CH_SPACE_NAME if declared)
    # are present and the gate can prove Mongo + Storage connectivity.
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
