# Copyright (c) 2026 ChatHealthy.ai LLC. All rights reserved.
# Licensed under the FindCare Evaluation License (FEL-1.0).

"""checkPipelineStatus -- render a run's status FROM THE RUN-STATUS API.

EPIC-010-F-001-S-015: the status API is where status comes from. The API is
one generic, step-metadata-driven interface serving every pipeline; ALL status
assembly -- step states (including 'skipped' for steps a start_at sliced
away), progress, the run state and its explanation -- happens there. This
client holds NO business logic and reads NO data store: it presents
claudeCodeAgent's certificate to the controller's mTLS listener, GETs
/status?run_id=<id>, and renders the document it receives. It knows nothing
about any pipeline.

Usage:
    python checkPipelineStatus.py <run_id> [--host <controller-address>]

Host resolution: --host, else CH_PIPELINE_STATUS_HOST env, else the static
controller public IP read from Azure. The client credential is the
claudeCodeAgent certificate from the pipeline Key Vault; the server is
verified against the ChatHealthy CA chain from the same vault.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import ssl
import subprocess
import sys
import tempfile
import urllib.request

# Runnable from the repository root: the lib lives beside the pipeline, not
# beside this file (same bootstrap as the sibling ops tools).
_HERE = pathlib.Path(__file__).resolve()
for _parent in _HERE.parents:
    if (_parent / "pipeline").is_dir():
        sys.path.insert(0, str(_parent))
        sys.path.insert(0, str(_parent / "ChatHealthyLib" / "src"))
        break

from chathealthy_lib.exceptions import ChatHealthyException  # noqa: E402

STATUS_PORT = 6969
_VAULT = "kv-chpipeline-dev"
_PIP_NAME = "pip-chathealthy-pipeline-dev"
_PIP_RG = "rg-chathealthy-pipeline-dev"


def _az(args: list[str]) -> str:
    import shutil  # noqa: PLC0415
    exe = shutil.which("az")
    if not exe:
        raise ChatHealthyException(
            mode="config_error", component="checkPipelineStatus",
            message="az CLI not found; pass --host or set CH_PIPELINE_STATUS_HOST")
    p = subprocess.run([exe, *args], capture_output=True, text=True, shell=False)
    if p.returncode != 0:
        raise ChatHealthyException(
            mode="runtime_error", component="checkPipelineStatus",
            message=f"az {' '.join(args[:3])}... failed: {p.stderr[:300]}")
    return p.stdout.strip()


def _resolve_host(cli_host: str | None) -> str:
    host = cli_host or os.environ.get("CH_PIPELINE_STATUS_HOST", "").strip()
    if host:
        return host
    return _az(["network", "public-ip", "show", "-g", _PIP_RG, "-n", _PIP_NAME,
                "--query", "ipAddress", "-o", "tsv"])


def _vault_secret(name: str) -> str:
    return _az(["keyvault", "secret", "show", "--vault-name", _VAULT,
                "--name", name, "--query", "value", "-o", "tsv"])


def _temp_pem(content: str, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="chstatus_")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _ssl_context() -> ssl.SSLContext:
    """mTLS as claudeCodeAgent: client cert+key from the identity's vault
    secret, server verified against the ChatHealthy CA chain."""
    bundle = _vault_secret("claudeCodeAgent")
    if "PRIVATE KEY" not in bundle:
        raise ChatHealthyException(
            mode="config_error", component="checkPipelineStatus",
            message="vault secret 'claudeCodeAgent' carries no private key; "
                    "the client cannot present the identity")
    chain = _vault_secret("ca-intermediate-cert") + "\n" + _vault_secret("ca-root-cert")
    cert_path = _temp_pem(bundle, "_id.pem")
    ca_path = _temp_pem(chain, "_ca.pem")
    ctx = ssl.create_default_context(cafile=ca_path)
    ctx.check_hostname = False  # the CA-signed server cert names the VM, not the IP
    ctx.load_cert_chain(cert_path)
    return ctx


def _render(doc: dict, out) -> None:
    """Rendering only: scalars as header lines, steps as the table. The field
    set is the API's; nothing here interprets or recomputes."""
    steps = doc.get("steps") or []
    for key, value in doc.items():
        if key == "steps":
            continue
        if isinstance(value, (dict, list)):
            out(f"  {key}={json.dumps(value, default=str)[:220]}\n")
        else:
            out(f"  {key}={value}\n")
    out(f"| {'step':38s} | {'state':11s} | {'progress':24s} |\n")
    out(f"|{'-' * 40}|{'-' * 13}|{'-' * 26}|\n")
    for s in steps:
        name = str(s.get("step"))
        state = str(s.get("state"))
        total = s.get("total") or 0
        if total:
            prog = f"{s.get('done', 0)} done, {s.get('failed', 0)} failed / {total}"
        else:
            prog = "-"
        out(f"| {name:38s} | {state:11s} | {prog:24s} |\n")


def check(run_id: str, host: str | None = None, out=sys.stdout.write) -> int:
    url = f"https://{_resolve_host(host)}:{STATUS_PORT}/status?run_id={run_id}"
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=30, context=_ssl_context()) as r:
            doc = json.loads(r.read().decode("utf-8"))
    except Exception as exc:
        out(f"status API unreachable at {url}: {type(exc).__name__}: "
            f"{str(exc)[:200]}\n")
        out("(no controller is answering -- the run has no live controller, "
            "or the address is wrong)\n")
        return 2
    _render(doc, out)
    state = str(doc.get("run_state") or "")
    return 0 if state in ("complete", "working") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Render a pipeline run's status from the run-status API")
    parser.add_argument("run_id")
    parser.add_argument("--host", default=None,
                        help="Controller address; else CH_PIPELINE_STATUS_HOST "
                             "env, else the static controller public IP from Azure")
    ns = parser.parse_args(argv)
    return check(ns.run_id, ns.host)


if __name__ == "__main__":
    sys.exit(main())
