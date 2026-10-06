"""Expose the CA chain to an image build.

Reads the CA root and intermediate certificates from Key Vault and sets
them as base64 environment variables. The deploy passes those to
`az acr build --build-arg`; Dockerfile.control and Dockerfile.worker
decode them to /etc/chathealthy/ca/root.pem and intermediate.pem and
refuse the build unless both decode to a certificate. A container then
finds the chain on its own filesystem at boot with no Key Vault call.

Azure calls go through the subprocess `az` shim. Every one fails loud on
a non-zero exit with the tail of stderr; no fallbacks.
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
import sys as _ch_sys, pathlib as _ch_pl  # noqa: E402
for _ch_d in _ch_pl.Path(__file__).resolve().parents:
    if (_ch_d / '.git').exists():
        _ch_lib = _ch_d / 'ChatHealthyLib' / 'src'
        if str(_ch_lib) not in _ch_sys.path:
            _ch_sys.path.insert(0, str(_ch_lib))
        break
from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402


def _cflags() -> int:
    return subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _step(msg: str) -> None:
    sys.stdout.write(f"[cert] {msg}\n")
    sys.stdout.flush()


def _kv_uri_for_env(kv_target, env: str) -> str:
    for eb in kv_target.environments:
        if getattr(eb, "env_binding", None) == env:
            return eb.node_address
    raise ChatHealthyException(
        mode="aborted",
        component="cert_placement",
        message=f"ERROR: KV target has no env_binding for env {env!r}.")


def _kv_name_for_env(kv_target, env: str) -> str:
    """Extract 'kv-chpipeline-dev' from vault URI 'https://kv-chpipeline-dev.vault.azure.net/'."""
    uri = _kv_uri_for_env(kv_target, env)
    host = uri.split("://", 1)[-1].split(".", 1)[0]
    return host


def _block_field(block, key: str):
    """Read a field from an env block that may be a dict or an object."""
    if block is None:
        return None
    if isinstance(block, dict):
        return block.get(key)
    return getattr(block, key, None)


def _acr_name_for_env(acr_target, env: str) -> str:
    for eb in acr_target.environments:
        if getattr(eb, "env_binding", None) == env:
            block = getattr(eb, "azure_container_registry", None)
            name = _block_field(block, "registry_name")
            if not name:
                raise ChatHealthyException(
                    mode="aborted",
                    component="cert_placement",
                    message=f"ERROR: ACR target env_binding {env!r} missing "
                    f"azure_container_registry.registry_name.")
            return name
    raise ChatHealthyException(
        mode="aborted",
        component="cert_placement",
        message=f"ERROR: ACR target has no env_binding for env {env!r}.")


def bake_ca_chain_into_images(env: str, acr_target, kv_target) -> None:
    """Set CHATHEALTHY_CA_ROOT_B64 and CHATHEALTHY_CA_INTERMEDIATE_B64 in
    this process, read from the vault bound to this env."""
    acr_name = _acr_name_for_env(acr_target, env)
    vault_name = _kv_name_for_env(kv_target, env)
    _step(f"exposing CA chain as ACR build-arg for {acr_name}")
    root_b64, intermediate_b64 = _fetch_ca_pem_b64s(vault_name)
    os.environ["CHATHEALTHY_CA_ROOT_B64"] = root_b64
    os.environ["CHATHEALTHY_CA_INTERMEDIATE_B64"] = intermediate_b64


def _fetch_ca_pem_b64s(vault_name: str) -> tuple[str, str]:
    """Fetch ca-root-cert + ca-intermediate-cert; return (root_b64, int_b64)."""
    if os.environ.get("CHATHEALTHY_CERT_TEST_MODE") == "1":
        return (
            base64.b64encode(b"---MOCK ROOT---").decode("ascii"),
            base64.b64encode(b"---MOCK INTERMEDIATE---").decode("ascii"),
        )
    root = _kv_secret_value(vault_name, "ca-root-cert")
    intermediate = _kv_secret_value(vault_name, "ca-intermediate-cert")
    if "BEGIN CERTIFICATE" not in root or "END CERTIFICATE" not in root:
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: KV secret ca-root-cert in {vault_name} is not a PEM cert "
            f"(len={len(root)}).")
    if (
        "BEGIN CERTIFICATE" not in intermediate
        or "END CERTIFICATE" not in intermediate
    ):
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: KV secret ca-intermediate-cert in {vault_name} is not a "
            f"PEM cert (len={len(intermediate)}).")
    return (
        base64.b64encode(root.encode("utf-8")).decode("ascii"),
        base64.b64encode(intermediate.encode("utf-8")).decode("ascii"),
    )


def _kv_secret_value(vault_name: str, secret_name: str) -> str:
    r = subprocess.run(
        [
            "az", "keyvault", "secret", "show",
            "--vault-name", vault_name,
            "--name", secret_name,
            "--query", "value",
            "-o", "tsv",
        ],
        capture_output=True, text=True,
        creationflags=_cflags(), shell=(sys.platform == "win32"),
    )
    if r.returncode != 0 or not (r.stdout or "").strip():
        raise ChatHealthyException(
            mode="aborted",
            component="cert_placement",
            message=f"ERROR: cannot read KV secret {secret_name!r} from "
            f"{vault_name}: {(r.stderr or '').strip()[:800]}")
    return r.stdout.strip()


def mint_declared_identity_certs_if_missing(coll, env: str) -> None:
    """Issue each IdentityCatalog certificate that is declared but absent, in
    the vault's established form: the identity's credential is a PLAIN secret
    named <identity> holding the cert+key PEM (the connection utility reads it
    and its connection-fact tags), plus cert-<identity> (public PEM) and
    key-<identity> (private PEM). The certificate is issued by the ChatHealthy
    Root CA whose cert and key live in the same vault, so the server (Atlas)
    trusts it exactly as it trusts every other identity -- no per-identity CA
    registration. Idempotent: a present credential is left untouched. It grants
    nothing; the database user and its role stay manual entitlement work."""
    for ident in (coll.identity_catalog or []):
        cert = ident.get("certificate")
        if not cert:
            continue
        kv_target = coll.by_target_id(cert["vault_target_ref"])
        if kv_target is None:
            raise ChatHealthyException(
                mode="aborted",
                component="cert_placement",
                message=f"ERROR: identity {ident.get('identity_id')!r} names "
                f"vault_target_ref {cert['vault_target_ref']!r}, which is not a target.")
        vault_name = _kv_name_for_env(kv_target, env)
        ca_cert = _kv_secret_value(vault_name, "ca-root-cert")
        ca_key = _kv_secret_value(vault_name, "ca-root-privatekey")
        mgr = DeploymentCertManager(vault_name, ca_cert, ca_key)
        outcome = mgr.ensure(cert["vault_secret_name"], cert["subject_dn"])
        _step(f"identity certificate {cert['vault_secret_name']!r} in "
              f"{vault_name}: {outcome}")


class DeploymentCertManager:
    """Issues and stores one identity's certificate in one Key Vault.

    The credential is CA-issued from the ChatHealthy Root CA (cert + key
    supplied at construction), matching every other identity so the server
    trusts it with no per-identity CA registration. It is stored as the three
    plain secrets the connection utility and the deploy expect:
      <identity>        combined cert+key PEM (read by the connector; the deploy
                        tags its connection facts here)
      cert-<identity>   public certificate PEM only
      key-<identity>    private key PEM only

    ensure() leaves an existing identity credential untouched; rotate()
    replaces it. The issued leaf matches the reference identity: RSA 2048,
    1-year validity, sha256, subject DER order C,ST,O,CN, exactly two critical
    extensions (basicConstraints ca=False, keyUsage digital_signature +
    key_encipherment)."""

    def __init__(self, vault_name: str, ca_cert_pem: str, ca_key_pem: str) -> None:
        from cryptography import x509  # noqa: PLC0415
        from cryptography.hazmat.primitives import serialization  # noqa: PLC0415
        self._vault = vault_name
        self._ca_cert = x509.load_pem_x509_certificate(ca_cert_pem.encode("ascii"))
        self._ca_key = serialization.load_pem_private_key(
            ca_key_pem.encode("ascii"), password=None)

    def ensure(self, identity: str, subject_dn: str) -> str:
        """Issue the identity's cert set only if the <identity> secret is
        absent; an existing plain secret is left exactly as it is."""
        if self._identity_secret_present(identity):
            return "present"
        self._issue_and_store(identity, subject_dn)
        return "issued"

    def rotate(self, identity: str, subject_dn: str) -> str:
        """Replace the identity's cert set with a freshly issued one."""
        self._issue_and_store(identity, subject_dn)
        return "rotated"

    def _issue_and_store(self, identity: str, subject_dn: str) -> None:
        cert_pem, key_pem = self._ca_issue(identity, subject_dn)
        # A KV-managed certificate object owns the <identity> secret name and
        # blocks a plain secret there; clear any such object first.
        self._purge_cert_object(identity)
        combined = cert_pem.rstrip("\n") + "\n" + key_pem.rstrip("\n") + "\n"
        self._secret_set(identity, combined)
        self._secret_set(f"cert-{identity}", cert_pem.rstrip("\n") + "\n")
        self._secret_set(f"key-{identity}", key_pem.rstrip("\n") + "\n")

    def _ca_issue(self, identity: str, subject_dn: str) -> tuple[str, str]:
        import datetime  # noqa: PLC0415
        from cryptography import x509  # noqa: PLC0415
        from cryptography.hazmat.primitives import hashes, serialization  # noqa: PLC0415
        from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: PLC0415
        leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (
            x509.CertificateBuilder()
            .subject_name(self._subject_name(subject_dn, identity))
            .issuer_name(self._ca_cert.subject)
            .public_key(leaf_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + datetime.timedelta(days=365))
            .add_extension(
                x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(
                    digital_signature=True, content_commitment=False,
                    key_encipherment=True, data_encipherment=False,
                    key_agreement=False, key_cert_sign=False, crl_sign=False,
                    encipher_only=False, decipher_only=False),
                critical=True)
            .sign(self._ca_key, hashes.SHA256())
        )
        cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
        key_pem = leaf_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode("ascii")
        return cert_pem, key_pem

    def _subject_name(self, subject_dn: str, identity: str):
        """Build the subject in the DER attribute order the reference identity
        uses (C, ST, O, CN) so the server derives the same RFC-2253 DN."""
        from cryptography import x509  # noqa: PLC0415
        from cryptography.x509.oid import NameOID  # noqa: PLC0415
        parts: dict = {}
        for piece in subject_dn.split(","):
            if "=" in piece:
                k, v = piece.split("=", 1)
                parts[k.strip().upper()] = v.strip()
        ordered = [
            (NameOID.COUNTRY_NAME, parts.get("C")),
            (NameOID.STATE_OR_PROVINCE_NAME, parts.get("ST")),
            (NameOID.ORGANIZATION_NAME, parts.get("O")),
            (NameOID.COMMON_NAME, parts.get("CN") or identity),
        ]
        return x509.Name([x509.NameAttribute(o, v) for o, v in ordered if v])

    def _identity_secret_present(self, identity: str) -> bool:
        """True when a PLAIN secret <identity> exists (not a cert-managed one)."""
        r = subprocess.run(
            ["az", "keyvault", "secret", "show", "--vault-name", self._vault,
             "--name", identity, "--query", "managed", "-o", "tsv"],
            capture_output=True, text=True,
            creationflags=_cflags(), shell=(sys.platform == "win32"))
        if r.returncode != 0:
            return False
        return (r.stdout or "").strip().lower() != "true"

    def _purge_cert_object(self, name: str) -> None:
        import time  # noqa: PLC0415
        if subprocess.run(
                ["az", "keyvault", "certificate", "show", "--vault-name",
                 self._vault, "--name", name],
                capture_output=True, text=True, creationflags=_cflags(),
                shell=(sys.platform == "win32")).returncode != 0:
            return
        subprocess.run(
            ["az", "keyvault", "certificate", "delete", "--vault-name",
             self._vault, "--name", name],
            capture_output=True, text=True, creationflags=_cflags(),
            shell=(sys.platform == "win32"))
        for i in range(30):
            if subprocess.run(
                    ["az", "keyvault", "certificate", "show-deleted",
                     "--vault-name", self._vault, "--name", name],
                    capture_output=True, text=True, creationflags=_cflags(),
                    shell=(sys.platform == "win32")).returncode == 0:
                subprocess.run(
                    ["az", "keyvault", "certificate", "purge", "--vault-name",
                     self._vault, "--name", name],
                    capture_output=True, text=True, creationflags=_cflags(),
                    shell=(sys.platform == "win32"))
                _step(f"purged stale certificate object {name!r}")
                break
            _step(f"waiting on {name!r} certificate soft-delete (poll {i})")
            time.sleep(2)
        self._wait_secret_name_free(name)

    def _wait_secret_name_free(self, name: str) -> None:
        """The cert purge is eventually consistent; the backing secret stays
        certificate-associated for a moment, so a plain `secret set` is refused.
        Wait until the name resolves to NotFound."""
        import time  # noqa: PLC0415
        for i in range(30):
            if subprocess.run(
                    ["az", "keyvault", "secret", "show", "--vault-name",
                     self._vault, "--name", name],
                    capture_output=True, text=True, creationflags=_cflags(),
                    shell=(sys.platform == "win32")).returncode != 0:
                if i:
                    _step(f"secret name {name!r} free after {i} poll(s)")
                return
            _step(f"waiting for {name!r} secret name to free (poll {i})")
            time.sleep(2)

    def _secret_set(self, name: str, pem: str) -> None:
        fd, path = tempfile.mkstemp(suffix=".pem")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(pem.encode("ascii"))  # LF bytes; no CRLF translation
            r = subprocess.run(
                ["az", "keyvault", "secret", "set", "--vault-name", self._vault,
                 "--name", name, "--file", path, "--encoding", "utf-8"],
                capture_output=True, text=True, creationflags=_cflags(),
                shell=(sys.platform == "win32"))
            if r.returncode != 0:
                raise ChatHealthyException(
                    mode="aborted",
                    component="cert_placement",
                    message=f"ERROR: storing secret {name!r} in {self._vault} "
                    f"failed: {(r.stderr or '').strip()[:400]}")
            _step(f"stored secret {name!r} ({len(pem)} bytes)")
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


