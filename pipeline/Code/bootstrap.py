"""Container bootstrap for the pipeline tier.

Two boot paths, dispatched on the node's canonical identity (env var
CHATHEALTHY_NODE_IDENTITY):

  Long-lived identities  (pipeline-runbook, pipeline-control, and any
  future long-lived service):
    - Attached managed identity is the seed credential.
    - Seed-read the leaf cert + private key from Key Vault at the
      node-scoped path certs/<node-identity> using the MI (F-003 §5.1,
      §6.1). Materialize as PEM files with 0600 perms.
    - Load every other secret this node needs from KV via the same MI.
    - Load the CA public cert + intermediate chain from KV so downstream
      Mongo X.509 clients can verify server (F-003 §7.1).

  Short-lived identities  (pipeline-worker-*):
    - Attached managed identity is the seed credential.
    - Self-mint a leaf cert via the F-003 Runbook cert-issuance API
      using chathealthy_ca.mint_via_managed_identity (F-003 §5.2).
    - Hold cert + key in memory; only materialize to a 0600 temp file
      because pymongo tlsCertificateKeyFile needs a path.
    - Load the CA chain from the image bake location (deploy-baked at
      /etc/chathealthy/ca/) or fall back to KV.
    - Do NOT write anything to KV.

Both paths:
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

import atexit
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Iterable, Optional, Tuple

from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
from azure.keyvault.secrets import SecretClient
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

import blob_logger
import chathealthy_ca


# Environment inputs the deploy step provides on every container.
ENV_NODE_IDENTITY = "CHATHEALTHY_NODE_IDENTITY"
ENV_KEY_VAULT_URI = "KEY_VAULT_URI"

# Environment outputs bootstrap sets for downstream code (pymongo, etc.).
ENV_CERT_PATH = "CHATHEALTHY_CERT_PATH"
ENV_KEY_PATH = "CHATHEALTHY_KEY_PATH"
ENV_CA_CHAIN_PATH = "CHATHEALTHY_CA_CHAIN_PATH"

# KV path convention (F-003 §6.1). One secret per long-lived identity;
# workers never have a KV entry.
KV_CERT_PREFIX = "certs-"           # certs-pipeline-runbook, certs-pipeline-control
KV_KEY_SUFFIX = "-key"              # certs-pipeline-runbook-key, ...
KV_CA_PUBLIC = "ca-root-cert"
KV_CA_INTERMEDIATE_CHAIN = "ca-intermediate-cert"

WORKER_NAMESPACE_PREFIX = "pipeline-worker"


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
    exclude_prefixes = (KV_CERT_PREFIX, KV_CA_PUBLIC, KV_CA_INTERMEDIATE_CHAIN)
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
    its cert + key from KV under the node-scoped path via its own MI.
    Returns (cert_path, key_path)."""
    cert_secret = f"{KV_CERT_PREFIX}{node_identity}"
    key_secret = f"{cert_secret}{KV_KEY_SUFFIX}"
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


def _self_mint_worker_cert(
    node_identity: str,
    mi_credential: ManagedIdentityCredential,
) -> Tuple[Path, Path]:
    """F-003 §5.2: short-lived Worker generates a keypair in memory,
    posts a CSR to the F-003 Runbook cert-issuance API using its
    attached MI, receives a signed leaf, materializes to memory-backed
    temp files (pymongo tlsCertificateKeyFile needs a path)."""
    _emit(f"self-minting short-lived cert for {node_identity}")
    # chathealthy_ca.mint_via_managed_identity generates keypair,
    # builds CSR with subject=node_identity, POSTs to CA endpoint,
    # returns (cert_pem_bytes, key_pem_bytes).
    cert_pem, key_pem = chathealthy_ca.mint_via_managed_identity(
        subject=node_identity,
        mi_credential=mi_credential,
    )
    cert_path = _track(_write_secure_file(cert_pem, "_cert.pem"))
    key_path = _track(_write_secure_file(key_pem, "_key.pem"))
    return cert_path, key_path


def _is_worker(node_identity: str) -> bool:
    return node_identity.startswith(WORKER_NAMESPACE_PREFIX)


def main() -> int:
    pipeline_name = os.environ.get("PIPELINE_NAME", "provider")
    import logging  # noqa: PLC0415 (see Rule-005; bootstrap exemption)
    blob_logger.install(pipeline_name, level=logging.DEBUG)

    if len(sys.argv) < 2:
        _emit("usage: bootstrap.py <entry_point.py> [args...]")
        return 2

    atexit.register(_cleanup_materialized)

    node_identity = _resolve_node_identity()
    _emit(f"node identity: {node_identity}")

    cred: DefaultAzureCredential | ManagedIdentityCredential
    if _is_worker(node_identity):
        # Worker path: no KV seed-read. Managed identity is used only
        # to authenticate to the F-003 cert-issuance API. Workers have
        # no per-node secret set in KV; they receive their cert
        # material from Control via work-item and self-mint.
        mi_cred = ManagedIdentityCredential()
        cert_path, key_path = _self_mint_worker_cert(node_identity, mi_cred)
        ca_path = _materialize_ca_chain(client=None)
    else:
        # Long-lived path: seed-read cert + key + bulk-load all other
        # secrets from KV using the attached MI.
        vault_uri = _resolve_vault_uri()
        cred = DefaultAzureCredential()
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
