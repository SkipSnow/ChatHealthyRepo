"""Cert bootstrap for Azure Automation Runbooks.

Runbooks run in the Azure Automation sandbox (not Docker), so they cannot use
the container bootstrap.py. This module provides the same cert-fetching logic
for runbooks to seed-read pre-provisioned "pipelineEditor" certs from Key Vault
and materialize them as temp files for pymongo.

Usage:
    import runbook_bootstrap
    runbook_bootstrap.materialize_certs_and_set_env()
    # Now CHATHEALTHY_CERT_PATH and CHATHEALTHY_KEY_PATH are set
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient


_materialized_paths: list[Path] = []



def _ch_exc():
    """ChatHealthyException without assuming the library is installed.
    These modules run as bare scripts in the devops chain."""
    import sys as _s, pathlib as _p
    for _d in _p.Path(__file__).resolve().parents:
        if (_d / ".git").exists():
            _l = _d / "ChatHealthyLib" / "src"
            if str(_l) not in _s.path:
                _s.path.insert(0, str(_l))
            break
    from chathealthy_lib.exceptions import ChatHealthyException
    return ChatHealthyException


def _cleanup_on_exit() -> None:
    """Clean up temp files on process exit."""
    for p in _materialized_paths:
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


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
        pass
    _materialized_paths.append(path)
    return path


def materialize_certs_and_set_env(
    vault_uri: str | None = None,
    cert_secret: str = "cert-pipelineEditor",
    key_secret: str = "key-pipelineEditor",
) -> None:
    """Fetch cert + key from Key Vault and set environment variables.

    Uses the Automation Account's managed identity to authenticate to KV.
    Materializes cert/key to temp files and sets:
      CHATHEALTHY_CERT_PATH
      CHATHEALTHY_KEY_PATH

    Args:
        vault_uri: Key Vault URI (e.g., https://kv-chpipeline-dev.vault.azure.net/).
                   If not provided, uses KEY_VAULT_URI env var.
        cert_secret: KV secret name for the cert (default: "cert-pipelineEditor")
        key_secret: KV secret name for the key (default: "key-pipelineEditor")

    Raises:
        ValueError: If vault_uri cannot be resolved or KV secrets are missing.
    """
    import atexit
    atexit.register(_cleanup_on_exit)

    if not vault_uri:
        vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
        if not vault_uri:
            raise _ch_exc()(
            mode="value_error",
            component="runbook_bootstrap",
            message="vault_uri not provided and KEY_VAULT_URI env var not set")

    try:
        cred = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_uri, credential=cred)
    except Exception as exc:
        raise _ch_exc()(
            mode="value_error",
            component="runbook_bootstrap",
            message=f"Failed to create SecretClient for {vault_uri}: {exc}") from exc

    try:
        cert_pem = client.get_secret(cert_secret).value or ""
        key_pem = client.get_secret(key_secret).value or ""
    except Exception as exc:
        raise _ch_exc()(
            mode="value_error",
            component="runbook_bootstrap",
            message=f"Failed to fetch secrets {cert_secret}/{key_secret} from KV: {exc}") from exc

    if not cert_pem or not key_pem:
        raise _ch_exc()(
            mode="value_error",
            component="runbook_bootstrap",
            message=f"KV secrets {cert_secret} or {key_secret} are empty")

    cert_path = _write_secure_file(cert_pem.encode("utf-8"), "_cert.pem")
    key_path = _write_secure_file(key_pem.encode("utf-8"), "_key.pem")

    os.environ["CHATHEALTHY_CERT_PATH"] = str(cert_path)
    os.environ["CHATHEALTHY_KEY_PATH"] = str(key_path)
