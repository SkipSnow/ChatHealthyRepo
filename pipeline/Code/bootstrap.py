"""Container bootstrap: fetch every secret from Azure Key Vault using
the attached managed identity, materialize as env vars (dash → underscore
to reverse the .env → KV name mapping), then exec the pipeline entry
point passed as argv[1]. Argv[2:] forwards to the entry point."""
from __future__ import annotations

import os
import sys

from azure.identity import DefaultAzureCredential
from azure.keyvault.secrets import SecretClient

import blob_logger


def kv_name_to_env_key(kv_name: str) -> str:
    return kv_name.replace("-", "_")


def main() -> int:
    # DEBUG-level logging into daily blob + stdout, tagged with pipeline name
    pipeline_name = os.environ.get("PIPELINE_NAME", "provider")
    import logging
    blob_logger.install(pipeline_name, level=logging.DEBUG)
    if len(sys.argv) < 2:
        print("bootstrap.py: usage: bootstrap.py <entry_point.py> [args...]",
              file=sys.stderr)
        return 2

    vault_uri = os.environ.get("KEY_VAULT_URI", "").strip()
    if not vault_uri:
        print("bootstrap.py: KEY_VAULT_URI env var is required", file=sys.stderr)
        return 2

    print(f"bootstrap: fetching secrets from {vault_uri}", flush=True)
    cred = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_uri, credential=cred)

    n = 0
    for secret_properties in client.list_properties_of_secrets():
        s = client.get_secret(secret_properties.name)
        env_key = kv_name_to_env_key(s.name)
        # Try to recover the original env-var name via the env_key tag, else
        # use the dash→underscore reversal.
        tag = (s.properties.tags or {}).get("env_key")
        if tag:
            env_key = tag
        os.environ[env_key] = s.value or ""
        n += 1
    print(f"bootstrap: loaded {n} secrets into env", flush=True)

    entry_point = sys.argv[1]
    forward_args = sys.argv[2:]
    print(f"bootstrap: exec python {entry_point} {' '.join(forward_args)}",
          flush=True)
    os.execvp("python", ["python", entry_point, *forward_args])


if __name__ == "__main__":
    raise SystemExit(main())
